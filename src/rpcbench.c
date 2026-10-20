/*
 * rpcbench - request/response network benchmark used to compare IRQ / application
 * CPU placement strategies.
 *
 *   rpcbench server [options]   runs on the machine under test (DUT)
 *   rpcbench client [options]   runs on a separate load-generator host
 *
 * Every request carries the client's (scheduled) send timestamp, so round-trip
 * latency is measured with a single clock on the client.  The server performs a
 * configurable amount of cache-touching "work" per request and records its own
 * per-request service time, which exposes interference from interrupts/softirqs
 * that land on the application's cores (stolen cycles + cache pollution).
 *
 * Both sides print a single JSON object on stdout when they finish.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/filter.h>
#include <math.h>
#include <net/if.h>
#include <netdb.h>
#include <linux/if_packet.h>
#include <linux/if_ether.h>
#include <poll.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define MAX_CPUS 1024
#define MAX_MSG 65536
#define HDR_SIZE 16

static volatile sig_atomic_t g_stop;

static void on_signal(int sig) { (void)sig; g_stop = 1; }

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static inline uint64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static inline uint64_t xorshift64(uint64_t *s)
{
	uint64_t x = *s;
	x ^= x << 13;
	x ^= x >> 7;
	x ^= x << 17;
	return *s = x;
}

/* ------------------------------------------------------------------------ */
/* Log-linear histogram: 32 sub-buckets per power of two (~3% resolution).   */

#define SUB_BITS 5
#define SUB (1 << SUB_BITS)
#define NBUCKETS (64 * SUB)

typedef struct {
	uint64_t c[NBUCKETS];
	uint64_t n, max;
	double sum;
} hist_t;

static inline int hist_idx(uint64_t v)
{
	if (v < SUB)
		return (int)v;
	int msb = 63 - __builtin_clzll(v);
	int shift = msb - SUB_BITS;
	return (shift + 1) * SUB + (int)((v >> shift) & (SUB - 1));
}

static uint64_t hist_value(int idx)
{
	if (idx < SUB)
		return idx;
	int shift = idx / SUB - 1;
	uint64_t lo = (uint64_t)(SUB + idx % SUB) << shift;
	return lo + ((1ull << shift) >> 1);
}

static inline void hist_add(hist_t *h, uint64_t v)
{
	h->c[hist_idx(v)]++;
	h->n++;
	h->sum += (double)v;
	if (v > h->max)
		h->max = v;
}

static void hist_merge(hist_t *dst, const hist_t *src)
{
	for (int i = 0; i < NBUCKETS; i++)
		dst->c[i] += src->c[i];
	dst->n += src->n;
	dst->sum += src->sum;
	if (src->max > dst->max)
		dst->max = src->max;
}

static uint64_t hist_pct(const hist_t *h, double p)
{
	if (!h->n)
		return 0;
	uint64_t target = (uint64_t)ceil(p / 100.0 * (double)h->n);
	if (target < 1)
		target = 1;
	uint64_t cum = 0;
	for (int i = 0; i < NBUCKETS; i++) {
		cum += h->c[i];
		if (cum >= target) {
			uint64_t v = hist_value(i);
			return v > h->max ? h->max : v;
		}
	}
	return h->max;
}

static void hist_json(FILE *f, const char *name, const hist_t *h)
{
	fprintf(f,
		"\"%s\":{\"count\":%llu,\"mean_ns\":%.1f,\"p50_ns\":%llu,\"p90_ns\":%llu,"
		"\"p99_ns\":%llu,\"p999_ns\":%llu,\"p9999_ns\":%llu,\"max_ns\":%llu}",
		name, (unsigned long long)h->n, h->n ? h->sum / (double)h->n : 0.0,
		(unsigned long long)hist_pct(h, 50), (unsigned long long)hist_pct(h, 90),
		(unsigned long long)hist_pct(h, 99), (unsigned long long)hist_pct(h, 99.9),
		(unsigned long long)hist_pct(h, 99.99), (unsigned long long)h->max);
}

/* ------------------------------------------------------------------------ */

static int parse_cpulist(const char *s, int *out, int max)
{
	int n = 0;
	char *dup = strdup(s), *save = NULL;
	for (char *tok = strtok_r(dup, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
		int a, b;
		if (sscanf(tok, "%d-%d", &a, &b) == 2) {
		} else if (sscanf(tok, "%d", &a) == 1) {
			b = a;
		} else {
			fprintf(stderr, "bad cpu list: %s\n", s);
			exit(1);
		}
		for (int c = a; c <= b && n < max; c++)
			out[n++] = c;
	}
	free(dup);
	return n;
}

static void pin_self(int cpu)
{
	cpu_set_t set;
	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	int rc = pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
	if (rc) {
		errno = rc;
		die("pthread_setaffinity_np");
	}
}

static void set_nonblock(int fd)
{
	int fl = fcntl(fd, F_GETFL);
	if (fl < 0 || fcntl(fd, F_SETFL, fl | O_NONBLOCK) < 0)
		die("fcntl");
}

static int write_full(int fd, const char *buf, size_t len)
{
	size_t off = 0;
	while (off < len) {
		ssize_t w = send(fd, buf + off, len - off, MSG_NOSIGNAL);
		if (w > 0) {
			off += w;
		} else if (w < 0 && (errno == EAGAIN || errno == EINTR)) {
			if (g_stop)
				return -1;
		} else {
			return -1;
		}
	}
	return 0;
}

/* ------------------------------------------------------------------------ */
/* Options                                                                   */

struct opts {
	int mode; /* 0 server, 1 client, 2 pub, 3 sub, 4 tap */
	int tcp;
	int port;
	char host[256];
	int nthreads;
	int cpus[MAX_CPUS];
	int ncpus;
	int req_size;
	int resp_size;
	/* server */
	int spin;
	int cpu_steer;
	int work_lines;
	int wss_kb;
	double max_seconds;
	/* client */
	int conns;
	double rate;
	int poisson;
	int outstanding;
	double duration;
	double warmup;
	int timeout_ms;
	/* feed modes */
	int burst, flows, ttl, busy_poll;
	int seq_offset, ts_offset, flow_offset;
	char payload[256], mcast_if[64], groups[1024], iface[64], stats_file[512];
};

static struct opts O = {
	.port = 9000,
	.host = "127.0.0.1",
	.nthreads = 1,
	.req_size = 64,
	.resp_size = 64,
	.work_lines = 256,
	.wss_kb = 512,
	.conns = 8,
	.outstanding = 1,
	.duration = 10,
	.warmup = 2,
	.timeout_ms = 200,
	.burst = 1,
	.flows = 1,
	.ttl = 8,
	.seq_offset = -2,
	.ts_offset = -2,
	.flow_offset = -2,
};

/* ------------------------------------------------------------------------ */
/* Server                                                                    */

struct srv_conn {
	int fd;
	int have;
	char buf[MAX_MSG];
};

struct srv_thread {
	pthread_t tid;
	int idx, cpu, lfd;
	hist_t svc;  /* recv returned -> reply sent */
	hist_t work; /* compute portion only */
	uint64_t requests;
	long nvcsw, nivcsw;
	uint64_t sink;
};

static atomic_int g_ready;

static inline uint64_t do_work(char *buf, size_t nlines, int lines, uint64_t *rng)
{
	uint64_t acc = 0;
	for (int i = 0; i < lines; i++) {
		uint64_t *p = (uint64_t *)(buf + (xorshift64(rng) % nlines) * 64);
		acc += *p;
		*p = acc ^ i;
	}
	return acc;
}

static int reuseport_socket(int type)
{
	int fd = socket(AF_INET, type, 0);
	if (fd < 0)
		die("socket");
	int one = 1, big = 4 << 20;
	setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	if (setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one)) < 0)
		die("SO_REUSEPORT");
	setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
	setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &big, sizeof(big));
	struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(O.port),
				.sin_addr.s_addr = htonl(INADDR_ANY)};
	if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0)
		die("bind");
	return fd;
}

/*
 * Steer each request to the server thread pinned on the CPU that ran the
 * kernel's receive processing (IRQ/softirq/RPS CPU), using a classic BPF
 * reuseport program: "if cpu == cpus[i] return i".  CPUs without a thread
 * return an out-of-range index and the kernel falls back to hashing.  This is
 * what makes "same" placement actually cache/wakeup-local on multi-queue NICs.
 */
static void attach_cpu_steering(int fd, struct srv_thread *ts)
{
	int n = O.nthreads, len = 2 * n + 2;
	struct sock_filter *code = calloc(len, sizeof(*code));
	code[0] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS, SKF_AD_OFF + SKF_AD_CPU);
	for (int i = 0; i < n; i++) {
		code[1 + 2 * i] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, ts[i].cpu, 0, 1);
		code[2 + 2 * i] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, i);
	}
	code[len - 1] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K, 0xffffffff);
	struct sock_fprog prog = {.len = len, .filter = code};
	if (setsockopt(fd, SOL_SOCKET, SO_ATTACH_REUSEPORT_CBPF, &prog, sizeof(prog)) < 0)
		die("SO_ATTACH_REUSEPORT_CBPF");
	free(code);
}

static void *server_thread(void *arg)
{
	struct srv_thread *t = arg;
	pin_self(t->cpu);

	size_t wss = (size_t)O.wss_kb * 1024;
	size_t nlines = wss / 64 ? wss / 64 : 1;
	char *buf = aligned_alloc(64, nlines * 64);
	if (!buf)
		die("aligned_alloc");
	memset(buf, 1, nlines * 64);
	uint64_t rng = 0x9E3779B97F4A7C15ull ^ (uint64_t)(t->idx + 1) * 0xBF58476D1CE4E5B9ull;
	char reply[MAX_MSG];
	memset(reply, 0, sizeof(reply));

	int ep = epoll_create1(0);
	if (ep < 0)
		die("epoll_create1");
	int lfd = t->lfd;
	struct epoll_event ev = {.events = EPOLLIN, .data.ptr = NULL};
	if (epoll_ctl(ep, EPOLL_CTL_ADD, lfd, &ev) < 0)
		die("epoll_ctl");
	atomic_fetch_add(&g_ready, 1);

	struct epoll_event evs[64];
	char rx[MAX_MSG];
	while (!g_stop) {
		int n = epoll_wait(ep, evs, 64, O.spin ? 0 : 100);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			die("epoll_wait");
		}
		for (int e = 0; e < n; e++) {
			struct srv_conn *c = evs[e].data.ptr;
			if (!O.tcp) {
				/* drain the UDP socket */
				for (;;) {
					struct sockaddr_storage from;
					socklen_t fl = sizeof(from);
					ssize_t r = recvfrom(lfd, rx, sizeof(rx), MSG_DONTWAIT,
							     (struct sockaddr *)&from, &fl);
					if (r < 0)
						break;
					uint64_t t0 = now_ns();
					t->sink += do_work(buf, nlines, O.work_lines, &rng);
					uint64_t t1 = now_ns();
					memcpy(reply, rx, r < HDR_SIZE ? r : HDR_SIZE);
					sendto(lfd, reply, O.resp_size, MSG_DONTWAIT,
					       (struct sockaddr *)&from, fl);
					uint64_t t2 = now_ns();
					hist_add(&t->work, t1 - t0);
					hist_add(&t->svc, t2 - t0);
					t->requests++;
				}
				continue;
			}
			if (!c) { /* TCP listener */
				for (;;) {
					int cfd = accept4(lfd, NULL, NULL, SOCK_NONBLOCK);
					if (cfd < 0)
						break;
					int one = 1;
					setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
					struct srv_conn *nc = calloc(1, sizeof(*nc));
					nc->fd = cfd;
					struct epoll_event cev = {.events = EPOLLIN, .data.ptr = nc};
					epoll_ctl(ep, EPOLL_CTL_ADD, cfd, &cev);
				}
				continue;
			}
			for (;;) {
				ssize_t r = recv(c->fd, c->buf + c->have, sizeof(c->buf) - c->have, 0);
				if (r == 0 || (r < 0 && errno != EAGAIN && errno != EINTR)) {
					epoll_ctl(ep, EPOLL_CTL_DEL, c->fd, NULL);
					close(c->fd);
					free(c);
					break;
				}
				if (r < 0)
					break;
				c->have += r;
				int off = 0;
				while (c->have - off >= O.req_size) {
					uint64_t t0 = now_ns();
					t->sink += do_work(buf, nlines, O.work_lines, &rng);
					uint64_t t1 = now_ns();
					memcpy(reply, c->buf + off, HDR_SIZE);
					write_full(c->fd, reply, O.resp_size);
					uint64_t t2 = now_ns();
					hist_add(&t->work, t1 - t0);
					hist_add(&t->svc, t2 - t0);
					t->requests++;
					off += O.req_size;
				}
				memmove(c->buf, c->buf + off, c->have - off);
				c->have -= off;
			}
		}
	}

	struct rusage ru;
	getrusage(RUSAGE_THREAD, &ru);
	t->nvcsw = ru.ru_nvcsw;
	t->nivcsw = ru.ru_nivcsw;
	close(lfd);
	close(ep);
	free(buf);
	return NULL;
}

static int run_server(void)
{
	struct srv_thread *ts = calloc(O.nthreads, sizeof(*ts));
	for (int i = 0; i < O.nthreads; i++) {
		ts[i].idx = i;
		ts[i].cpu = O.cpus[i % O.ncpus];
		/* sockets join the reuseport group in index order, which the BPF program relies on */
		ts[i].lfd = reuseport_socket(O.tcp ? SOCK_STREAM : SOCK_DGRAM);
		if (O.tcp && listen(ts[i].lfd, 1024) < 0)
			die("listen");
		set_nonblock(ts[i].lfd);
	}
	if (O.cpu_steer)
		attach_cpu_steering(ts[0].lfd, ts);
	for (int i = 0; i < O.nthreads; i++) {
		if (pthread_create(&ts[i].tid, NULL, server_thread, &ts[i]))
			die("pthread_create");
	}
	while (atomic_load(&g_ready) < O.nthreads)
		usleep(1000);
	fprintf(stderr, "READY\n");
	fflush(stderr);

	uint64_t start = now_ns();
	while (!g_stop) {
		usleep(10000);
		if (O.max_seconds > 0 && (now_ns() - start) / 1e9 >= O.max_seconds)
			g_stop = 1;
	}
	hist_t svc = {0}, work = {0};
	for (int i = 0; i < O.nthreads; i++) {
		pthread_join(ts[i].tid, NULL);
		hist_merge(&svc, &ts[i].svc);
		hist_merge(&work, &ts[i].work);
	}

	printf("{\"role\":\"server\",\"proto\":\"%s\",\"mode\":\"%s\",\"threads\":%d,"
	       "\"work_lines\":%d,\"wss_kb\":%d,\"cpu_steer\":%s,",
	       O.tcp ? "tcp" : "udp", O.spin ? "spin" : "block", O.nthreads, O.work_lines,
	       O.wss_kb, O.cpu_steer ? "true" : "false");
	hist_json(stdout, "service", &svc);
	putchar(',');
	hist_json(stdout, "work", &work);
	printf(",\"per_thread\":[");
	for (int i = 0; i < O.nthreads; i++)
		printf("%s{\"cpu\":%d,\"requests\":%llu,\"nvcsw\":%ld,\"nivcsw\":%ld}", i ? "," : "",
		       ts[i].cpu, (unsigned long long)ts[i].requests, ts[i].nvcsw, ts[i].nivcsw);
	printf("]}\n");
	fflush(stdout);
	return 0;
}

/* ------------------------------------------------------------------------ */
/* Client                                                                    */

struct cli_conn {
	int fd;
	int have;
	int inflight;
	uint64_t last_progress;
	char buf[MAX_MSG];
};

struct cli_thread {
	pthread_t tid;
	int idx, cpu;
	hist_t rtt;
	uint64_t sent, recvd, sent_win, recvd_win, timeouts, send_errs;
	uint64_t max_lag_ns;
};

static struct sockaddr_in g_srv;
static uint64_t g_t0, g_tw, g_te;

static int cli_send(struct cli_conn *c, char *msg, uint64_t seq, uint64_t ts, struct cli_thread *t)
{
	memcpy(msg, &seq, 8);
	memcpy(msg + 8, &ts, 8);
	int rc;
	if (O.tcp)
		rc = write_full(c->fd, msg, O.req_size);
	else
		rc = send(c->fd, msg, O.req_size, MSG_DONTWAIT) == O.req_size ? 0 : -1;
	if (rc) {
		t->send_errs++;
		return -1;
	}
	t->sent++;
	if (ts >= g_tw && ts < g_te)
		t->sent_win++;
	c->inflight++;
	return 0;
}

static void cli_complete(struct cli_thread *t, struct cli_conn *c, const char *msg, uint64_t now)
{
	uint64_t ts;
	memcpy(&ts, msg + 8, 8);
	t->recvd++;
	if (c->inflight > 0)
		c->inflight--;
	c->last_progress = now;
	if (ts >= g_tw && ts < g_te) {
		t->recvd_win++;
		hist_add(&t->rtt, now - ts);
	}
}

static void *client_thread(void *arg)
{
	struct cli_thread *t = arg;
	pin_self(t->cpu);

	struct cli_conn *cs = calloc(O.conns, sizeof(*cs));
	int ep = epoll_create1(0);
	for (int i = 0; i < O.conns; i++) {
		int fd = socket(AF_INET, O.tcp ? SOCK_STREAM : SOCK_DGRAM, 0);
		if (fd < 0)
			die("socket");
		int big = 4 << 20, one = 1;
		setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
		setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &big, sizeof(big));
		if (O.tcp)
			setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
		if (connect(fd, (struct sockaddr *)&g_srv, sizeof(g_srv)) < 0)
			die("connect");
		set_nonblock(fd);
		cs[i].fd = fd;
		struct epoll_event ev = {.events = EPOLLIN, .data.ptr = &cs[i]};
		epoll_ctl(ep, EPOLL_CTL_ADD, fd, &ev);
	}

	char msg[MAX_MSG];
	memset(msg, 0, sizeof(msg));
	uint64_t seq = (uint64_t)t->idx << 48;
	uint64_t rng = 0x2545F4914F6CDD1Dull ^ (uint64_t)(t->idx + 7) * 0x9E3779B97F4A7C15ull;
	double per_thread_rate = O.rate / O.nthreads;
	double interval = per_thread_rate > 0 ? 1e9 / per_thread_rate : 0;
	int rr = 0;

	while (now_ns() < g_t0)
		;
	double next = (double)g_t0 + (interval > 0 ? (double)(xorshift64(&rng) % 1000) / 1000.0 * interval : 0);

	if (interval == 0) { /* closed loop: prime every connection */
		uint64_t now = now_ns();
		for (int i = 0; i < O.conns; i++) {
			cs[i].last_progress = now;
			for (int k = 0; k < O.outstanding; k++)
				cli_send(&cs[i], msg, seq++, now_ns(), t);
		}
	}

	uint64_t drain_end = g_te + (uint64_t)O.timeout_ms * 1000000ull;
	uint64_t last_scan = 0;
	struct epoll_event evs[64];
	for (;;) {
		uint64_t now = now_ns();
		if (now >= drain_end || g_stop)
			break;
		int sending = now < g_te;

		if (interval > 0 && sending) {
			/* open loop: timestamp with the *scheduled* time to avoid coordinated omission */
			if ((double)now - next > (double)t->max_lag_ns)
				t->max_lag_ns = (uint64_t)((double)now - next);
			while ((double)now >= next && next < (double)g_te) {
				cli_send(&cs[rr], msg, seq++, (uint64_t)next, t);
				rr = (rr + 1) % O.conns;
				if (O.poisson) {
					double u = ((xorshift64(&rng) >> 11) + 0.5) / 9007199254740992.0;
					next += -log(u) * interval;
				} else {
					next += interval;
				}
			}
		}

		int n = epoll_wait(ep, evs, 64, 0);
		for (int e = 0; e < n; e++) {
			struct cli_conn *c = evs[e].data.ptr;
			if (!O.tcp) {
				for (;;) {
					ssize_t r = recv(c->fd, c->buf, sizeof(c->buf), MSG_DONTWAIT);
					if (r < HDR_SIZE)
						break;
					cli_complete(t, c, c->buf, now_ns());
					if (interval == 0 && sending)
						cli_send(c, msg, seq++, now_ns(), t);
				}
				continue;
			}
			for (;;) {
				ssize_t r = recv(c->fd, c->buf + c->have, sizeof(c->buf) - c->have, MSG_DONTWAIT);
				if (r <= 0)
					break;
				c->have += r;
				int off = 0;
				while (c->have - off >= O.resp_size) {
					cli_complete(t, c, c->buf + off, now_ns());
					off += O.resp_size;
					if (interval == 0 && sending)
						cli_send(c, msg, seq++, now_ns(), t);
				}
				memmove(c->buf, c->buf + off, c->have - off);
				c->have -= off;
			}
		}

		/* closed-loop UDP: re-prime connections whose requests were lost */
		now = now_ns();
		if (interval == 0 && sending && !O.tcp && now - last_scan > 1000000) {
			last_scan = now;
			for (int i = 0; i < O.conns; i++) {
				struct cli_conn *c = &cs[i];
				if (now - c->last_progress > (uint64_t)O.timeout_ms * 1000000ull) {
					t->timeouts += c->inflight;
					c->inflight = 0;
					c->last_progress = now;
					for (int k = 0; k < O.outstanding; k++)
						cli_send(c, msg, seq++, now_ns(), t);
				}
			}
		}
	}
	for (int i = 0; i < O.conns; i++)
		close(cs[i].fd);
	close(ep);
	free(cs);
	return NULL;
}

static int run_client(void)
{
	struct addrinfo hints = {.ai_family = AF_INET}, *res;
	int rc = getaddrinfo(O.host, NULL, &hints, &res);
	if (rc) {
		fprintf(stderr, "getaddrinfo(%s): %s\n", O.host, gai_strerror(rc));
		return 1;
	}
	g_srv = *(struct sockaddr_in *)res->ai_addr;
	g_srv.sin_port = htons(O.port);
	freeaddrinfo(res);

	/* give all threads time to connect before the clock starts */
	g_t0 = now_ns() + 200000000ull + (uint64_t)O.nthreads * O.conns * 1000000ull;
	g_tw = g_t0 + (uint64_t)(O.warmup * 1e9);
	g_te = g_tw + (uint64_t)(O.duration * 1e9);

	struct cli_thread *ts = calloc(O.nthreads, sizeof(*ts));
	for (int i = 0; i < O.nthreads; i++) {
		ts[i].idx = i;
		ts[i].cpu = O.cpus[i % O.ncpus];
		if (pthread_create(&ts[i].tid, NULL, client_thread, &ts[i]))
			die("pthread_create");
	}
	hist_t rtt = {0};
	uint64_t sent = 0, recvd = 0, sent_win = 0, recvd_win = 0, timeouts = 0, errs = 0, lag = 0;
	for (int i = 0; i < O.nthreads; i++) {
		pthread_join(ts[i].tid, NULL);
		hist_merge(&rtt, &ts[i].rtt);
		sent += ts[i].sent;
		recvd += ts[i].recvd;
		sent_win += ts[i].sent_win;
		recvd_win += ts[i].recvd_win;
		timeouts += ts[i].timeouts;
		errs += ts[i].send_errs;
		if (ts[i].max_lag_ns > lag)
			lag = ts[i].max_lag_ns;
	}
	double lost_pct = sent_win ? 100.0 * (double)(sent_win - (recvd_win < sent_win ? recvd_win : sent_win)) / (double)sent_win : 0;
	printf("{\"role\":\"client\",\"proto\":\"%s\",\"target_rate\":%.0f,\"threads\":%d,"
	       "\"conns_per_thread\":%d,\"outstanding\":%d,\"duration_s\":%.3f,"
	       "\"achieved_rps\":%.1f,\"sent\":%llu,\"received\":%llu,\"sent_in_window\":%llu,"
	       "\"received_in_window\":%llu,\"lost_pct\":%.4f,\"timeouts\":%llu,\"send_errors\":%llu,"
	       "\"max_sched_lag_ns\":%llu,",
	       O.tcp ? "tcp" : "udp", O.rate, O.nthreads, O.conns, O.outstanding, O.duration,
	       (double)recvd_win / O.duration, (unsigned long long)sent, (unsigned long long)recvd,
	       (unsigned long long)sent_win, (unsigned long long)recvd_win, lost_pct,
	       (unsigned long long)timeouts, (unsigned long long)errs, (unsigned long long)lag);
	hist_json(stdout, "rtt", &rtt);
	printf("}\n");
	fflush(stdout);
	return 0;
}

/* ------------------------------------------------------------------------ */
/* Feed modes: one-way, market-data style traffic                            */
/*                                                                          */
/*   pub  publishes UDP messages (unicast or multicast) at a paced rate, in   */
/*        bursts, over several flows.  Default message layout:               */
/*          [0..7] per-flow sequence  [8..15] CLOCK_REALTIME send ns          */
/*          [16..19] flow id                                                 */
/*   sub  a synthetic feed handler: pinned threads, optional busy polling,   */
/*        sequence-gap detection, one-way latency (needs synced clocks       */
/*        across hosts, e.g. PTP/chrony) and per-message work.               */
/*   tap  passive probe: AF_PACKET socket with kernel RX timestamps, giving  */
/*        one-way wire -> kernel-receive latency without touching the app.    */
/*                                                                          */
/* sub and tap reset their statistics on SIGUSR1 and write a JSON snapshot   */
/* to --stats-file on SIGUSR2, so a measurement window can be cut out of a    */
/* long-running process.                                                     */

#define MAX_FLOWS 4096

static volatile sig_atomic_t g_reset_req, g_dump_req;
static atomic_uint g_epoch;

static void on_usr1(int sig) { (void)sig; g_reset_req = 1; }
static void on_usr2(int sig) { (void)sig; g_dump_req = 1; }

static inline uint64_t real_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_REALTIME, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

static struct sockaddr_in resolve(const char *host, int port)
{
	struct addrinfo hints = {.ai_family = AF_INET}, *res;
	int rc = getaddrinfo(host, NULL, &hints, &res);
	if (rc) {
		fprintf(stderr, "getaddrinfo(%s): %s\n", host, gai_strerror(rc));
		exit(1);
	}
	struct sockaddr_in a = *(struct sockaddr_in *)res->ai_addr;
	a.sin_port = htons(port);
	freeaddrinfo(res);
	return a;
}

static void write_stats(void (*fn)(FILE *))
{
	if (!O.stats_file[0])
		return;
	char tmp[600];
	snprintf(tmp, sizeof(tmp), "%s.tmp", O.stats_file);
	FILE *f = fopen(tmp, "w");
	if (!f) {
		perror(tmp);
		return;
	}
	fn(f);
	fclose(f);
	rename(tmp, O.stats_file);
}

static void resolve_offsets(void)
{
	/* -2 = not given: default layout, or nothing when a payload template is used */
	int tmpl = O.payload[0] != 0;
	if (O.seq_offset == -2)
		O.seq_offset = tmpl ? -1 : 0;
	if (O.ts_offset == -2)
		O.ts_offset = tmpl ? -1 : 8;
	if (O.flow_offset == -2)
		O.flow_offset = tmpl ? -1 : 16;
}

/* ---- pub ---- */

static int run_pub(void)
{
	pin_self(O.cpus[0]);
	resolve_offsets();
	static char msg[MAX_MSG];
	int size = O.req_size;
	if (O.payload[0]) {
		FILE *f = fopen(O.payload, "rb");
		if (!f)
			die(O.payload);
		size = (int)fread(msg, 1, sizeof(msg), f);
		fclose(f);
	}
	struct sockaddr_in dst = resolve(O.host, O.port);
	int mc = IN_MULTICAST(ntohl(dst.sin_addr.s_addr));
	int *fds = calloc(O.flows, sizeof(int));
	uint64_t *seq = calloc(O.flows, sizeof(uint64_t));
	for (int f = 0; f < O.flows; f++) {
		int fd = socket(AF_INET, SOCK_DGRAM, 0);
		if (fd < 0)
			die("socket");
		int big = 4 << 20;
		setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &big, sizeof(big));
		if (mc) {
			unsigned char ttl = (unsigned char)O.ttl;
			setsockopt(fd, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl));
			if (O.mcast_if[0]) {
				struct in_addr ifa;
				inet_pton(AF_INET, O.mcast_if, &ifa);
				if (setsockopt(fd, IPPROTO_IP, IP_MULTICAST_IF, &ifa, sizeof(ifa)) < 0)
					die("IP_MULTICAST_IF");
			}
		}
		if (connect(fd, (struct sockaddr *)&dst, sizeof(dst)) < 0)
			die("connect");
		fds[f] = fd;
	}

	double interval = O.rate > 0 ? 1e9 * O.burst / O.rate : 0; /* ns between bursts */
	uint64_t start = now_ns() + 100000000ull;
	uint64_t end = start + (uint64_t)(O.duration * 1e9);
	double next = (double)start;
	uint64_t sent = 0, errs = 0, max_lag = 0;
	int rr = 0;
	while (now_ns() < start)
		;
	while (!g_stop) {
		uint64_t now = now_ns();
		if (now >= end)
			break;
		if (interval > 0) {
			if ((double)now < next) {
				double gap = next - (double)now;
				if (gap > 200000)
					usleep((useconds_t)(gap / 1000) - 100);
				continue;
			}
			if ((double)now - next > (double)max_lag)
				max_lag = (uint64_t)((double)now - next);
		}
		for (int b = 0; b < O.burst; b++) {
			int f = rr;
			rr = (rr + 1) % O.flows;
			if (O.seq_offset >= 0 && size >= O.seq_offset + 8)
				memcpy(msg + O.seq_offset, &seq[f], 8);
			if (O.flow_offset >= 0 && size >= O.flow_offset + 4) {
				uint32_t fid = (uint32_t)f;
				memcpy(msg + O.flow_offset, &fid, 4);
			}
			if (O.ts_offset >= 0 && size >= O.ts_offset + 8) {
				uint64_t ts = real_ns();
				memcpy(msg + O.ts_offset, &ts, 8);
			}
			if (send(fds[f], msg, size, 0) == size) {
				sent++;
				seq[f]++;
			} else {
				errs++;
			}
		}
		next += interval;
	}
	double secs = (double)(now_ns() - start) / 1e9;
	printf("{\"role\":\"pub\",\"host\":\"%s\",\"port\":%d,\"multicast\":%s,\"flows\":%d,\"burst\":%d,"
	       "\"size\":%d,\"target_mps\":%.0f,\"sent\":%llu,\"send_errors\":%llu,\"duration_s\":%.3f,"
	       "\"achieved_mps\":%.1f,\"max_sched_lag_ns\":%llu}\n",
	       O.host, O.port, mc ? "true" : "false", O.flows, O.burst, size, O.rate,
	       (unsigned long long)sent, (unsigned long long)errs, secs, secs > 0 ? sent / secs : 0,
	       (unsigned long long)max_lag);
	return 0;
}

/* ---- sub ---- */

struct sub_thread {
	pthread_t tid;
	int idx, cpu, fd;
	hist_t lat, work;
	uint64_t msgs, bytes, gaps, reorders, neg, sink;
	uint64_t last_seq[MAX_FLOWS];
};

static struct sub_thread *g_subs;

static void track(struct sub_thread *t, const char *buf, ssize_t r, uint64_t now)
{
	t->msgs++;
	t->bytes += r;
	if (O.ts_offset >= 0 && r >= O.ts_offset + 8) {
		uint64_t ts;
		memcpy(&ts, buf + O.ts_offset, 8);
		if (now >= ts)
			hist_add(&t->lat, now - ts);
		else
			t->neg++;
	}
	if (O.seq_offset >= 0 && r >= O.seq_offset + 8) {
		uint64_t s;
		uint32_t flow = 0;
		memcpy(&s, buf + O.seq_offset, 8);
		if (O.flow_offset >= 0 && r >= O.flow_offset + 4)
			memcpy(&flow, buf + O.flow_offset, 4);
		flow %= MAX_FLOWS;
		uint64_t last = t->last_seq[flow];
		if (last != UINT64_MAX) {
			if (s > last + 1)
				t->gaps += s - last - 1;
			else if (s <= last)
				t->reorders++;
		}
		if (last == UINT64_MAX || s > last)
			t->last_seq[flow] = s;
	}
}

static void *sub_thread_fn(void *arg)
{
	struct sub_thread *t = arg;
	pin_self(t->cpu);
	size_t nlines = (size_t)O.wss_kb * 1024 / 64;
	if (!nlines)
		nlines = 1;
	char *buf = aligned_alloc(64, nlines * 64);
	memset(buf, 1, nlines * 64);
	uint64_t rng = 0x9E3779B97F4A7C15ull ^ (uint64_t)(t->idx + 1);
	static __thread char rx[MAX_MSG];
	unsigned epoch = atomic_load(&g_epoch);
	struct pollfd pfd = {.fd = t->fd, .events = POLLIN};

	while (!g_stop) {
		unsigned e = atomic_load(&g_epoch);
		if (e != epoch) {
			epoch = e;
			memset(&t->lat, 0, sizeof(t->lat));
			memset(&t->work, 0, sizeof(t->work));
			t->msgs = t->bytes = t->gaps = t->reorders = t->neg = 0;
		}
		if (!O.spin && poll(&pfd, 1, 100) <= 0)
			continue;
		for (;;) {
			ssize_t r = recv(t->fd, rx, sizeof(rx), MSG_DONTWAIT);
			if (r < 0)
				break;
			track(t, rx, r, real_ns());
			if (O.work_lines > 0) {
				uint64_t t0 = now_ns();
				t->sink += do_work(buf, nlines, O.work_lines, &rng);
				hist_add(&t->work, now_ns() - t0);
			}
			if (!O.spin)
				continue;
			break; /* spinning: re-check the epoch between messages */
		}
	}
	free(buf);
	return NULL;
}

static void sub_json(FILE *f)
{
	hist_t lat = {0}, work = {0};
	uint64_t msgs = 0, bytes = 0, gaps = 0, reorders = 0, neg = 0;
	for (int i = 0; i < O.nthreads; i++) {
		hist_merge(&lat, &g_subs[i].lat);
		hist_merge(&work, &g_subs[i].work);
		msgs += g_subs[i].msgs;
		bytes += g_subs[i].bytes;
		gaps += g_subs[i].gaps;
		reorders += g_subs[i].reorders;
		neg += g_subs[i].neg;
	}
	fprintf(f, "{\"role\":\"sub\",\"mode\":\"%s\",\"threads\":%d,\"busy_poll_us\":%d,\"messages\":%llu,"
		   "\"bytes\":%llu,\"gaps\":%llu,\"reorders\":%llu,\"negative_latency\":%llu,",
		O.spin ? "spin" : "block", O.nthreads, O.busy_poll, (unsigned long long)msgs,
		(unsigned long long)bytes, (unsigned long long)gaps, (unsigned long long)reorders,
		(unsigned long long)neg);
	hist_json(f, "latency", &lat);
	fputc(',', f);
	hist_json(f, "work", &work);
	fprintf(f, ",\"per_thread\":[");
	for (int i = 0; i < O.nthreads; i++)
		fprintf(f, "%s{\"cpu\":%d,\"messages\":%llu,\"gaps\":%llu}", i ? "," : "", g_subs[i].cpu,
			(unsigned long long)g_subs[i].msgs, (unsigned long long)g_subs[i].gaps);
	fprintf(f, "]}\n");
}

static int sub_socket(int i, char groups[][64], int ngroups)
{
	int fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (fd < 0)
		die("socket");
	int one = 1, big = 16 << 20;
	setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
	setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
	if (O.busy_poll > 0)
		setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL, &O.busy_poll, sizeof(O.busy_poll));
	struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(O.port),
				.sin_addr.s_addr = htonl(INADDR_ANY)};
	if (ngroups) {
		/* one multicast group per thread (round robin); bind to the group to filter */
		const char *g = groups[i % ngroups];
		inet_pton(AF_INET, g, &a.sin_addr);
		struct ip_mreq mr = {.imr_interface.s_addr = htonl(INADDR_ANY)};
		inet_pton(AF_INET, g, &mr.imr_multiaddr);
		if (O.mcast_if[0])
			inet_pton(AF_INET, O.mcast_if, &mr.imr_interface);
		if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0)
			die("bind");
		if (setsockopt(fd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mr, sizeof(mr)) < 0)
			die("IP_ADD_MEMBERSHIP");
	} else if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0) {
		die("bind");
	}
	set_nonblock(fd);
	return fd;
}

static int run_sub(void)
{
	resolve_offsets();
	char groups[64][64];
	int ngroups = 0;
	if (O.groups[0]) {
		char *dup = strdup(O.groups), *save = NULL;
		for (char *g = strtok_r(dup, ",", &save); g && ngroups < 64; g = strtok_r(NULL, ",", &save))
			snprintf(groups[ngroups++], 64, "%s", g);
		free(dup);
	}
	g_subs = calloc(O.nthreads, sizeof(*g_subs));
	for (int i = 0; i < O.nthreads; i++) {
		g_subs[i].idx = i;
		g_subs[i].cpu = O.cpus[i % O.ncpus];
		memset(g_subs[i].last_seq, 0xff, sizeof(g_subs[i].last_seq));
		g_subs[i].fd = sub_socket(i, groups, ngroups);
	}
	if (O.cpu_steer && !ngroups) {
		struct srv_thread *ts = calloc(O.nthreads, sizeof(*ts));
		for (int i = 0; i < O.nthreads; i++)
			ts[i].cpu = g_subs[i].cpu;
		attach_cpu_steering(g_subs[0].fd, ts);
		free(ts);
	}
	for (int i = 0; i < O.nthreads; i++)
		if (pthread_create(&g_subs[i].tid, NULL, sub_thread_fn, &g_subs[i]))
			die("pthread_create");
	fprintf(stderr, "READY\n");
	fflush(stderr);

	uint64_t start = now_ns();
	while (!g_stop) {
		usleep(2000);
		if (g_reset_req) {
			g_reset_req = 0;
			atomic_fetch_add(&g_epoch, 1);
		}
		if (g_dump_req) {
			g_dump_req = 0;
			write_stats(sub_json);
		}
		if (O.max_seconds > 0 && (now_ns() - start) / 1e9 >= O.max_seconds)
			g_stop = 1;
	}
	for (int i = 0; i < O.nthreads; i++)
		pthread_join(g_subs[i].tid, NULL);
	write_stats(sub_json);
	sub_json(stdout);
	fflush(stdout);
	return 0;
}

/* ---- tap ---- */

static hist_t g_tap_lat;
static uint64_t g_tap_msgs, g_tap_neg;

static void tap_json(FILE *f)
{
	fprintf(f, "{\"role\":\"tap\",\"iface\":\"%s\",\"port\":%d,\"messages\":%llu,\"negative_latency\":%llu,",
		O.iface, O.port, (unsigned long long)g_tap_msgs, (unsigned long long)g_tap_neg);
	hist_json(f, "latency", &g_tap_lat);
	fprintf(f, "}\n");
}

static int run_tap(void)
{
	pin_self(O.cpus[0]);
	resolve_offsets();
	if (O.ts_offset < 0) {
		fprintf(stderr, "tap needs --ts-offset >= 0 (where the sender put its timestamp)\n");
		return 2;
	}
	int fd = socket(AF_PACKET, SOCK_DGRAM, htons(ETH_P_IP));
	if (fd < 0)
		die("socket(AF_PACKET)");
	/* accept only unfragmented UDP to --port */
	struct sock_filter code[] = {
		BPF_STMT(BPF_LD | BPF_B | BPF_ABS, 9),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, IPPROTO_UDP, 0, 6),
		BPF_STMT(BPF_LD | BPF_H | BPF_ABS, 6),
		BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x1fff, 4, 0),
		BPF_STMT(BPF_LDX | BPF_B | BPF_MSH, 0),
		BPF_STMT(BPF_LD | BPF_H | BPF_IND, 2),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (unsigned)O.port, 0, 1),
		BPF_STMT(BPF_RET | BPF_K, 0xffff),
		BPF_STMT(BPF_RET | BPF_K, 0),
	};
	struct sock_fprog prog = {.len = sizeof(code) / sizeof(code[0]), .filter = code};
	if (setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER, &prog, sizeof(prog)) < 0)
		die("SO_ATTACH_FILTER");
	struct sockaddr_ll sll = {.sll_family = AF_PACKET, .sll_protocol = htons(ETH_P_IP),
				  .sll_ifindex = (int)if_nametoindex(O.iface)};
	if (!sll.sll_ifindex) {
		fprintf(stderr, "unknown interface %s\n", O.iface);
		return 2;
	}
	if (bind(fd, (struct sockaddr *)&sll, sizeof(sll)) < 0)
		die("bind(AF_PACKET)");
	int on = 1, big = 16 << 20;
	setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPNS, &on, sizeof(on));
	setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
	fprintf(stderr, "READY\n");
	fflush(stderr);

	static char buf[MAX_MSG];
	char ctrl[256];
	struct pollfd pfd = {.fd = fd, .events = POLLIN};
	uint64_t start = now_ns();
	while (!g_stop) {
		if (g_reset_req) {
			g_reset_req = 0;
			memset(&g_tap_lat, 0, sizeof(g_tap_lat));
			g_tap_msgs = g_tap_neg = 0;
		}
		if (g_dump_req) {
			g_dump_req = 0;
			write_stats(tap_json);
		}
		if (O.max_seconds > 0 && (now_ns() - start) / 1e9 >= O.max_seconds)
			break;
		if (poll(&pfd, 1, 50) <= 0)
			continue;
		struct sockaddr_ll from;
		struct iovec iov = {.iov_base = buf, .iov_len = sizeof(buf)};
		struct msghdr mh = {.msg_name = &from, .msg_namelen = sizeof(from), .msg_iov = &iov,
				    .msg_iovlen = 1, .msg_control = ctrl, .msg_controllen = sizeof(ctrl)};
		ssize_t r = recvmsg(fd, &mh, MSG_DONTWAIT);
		if (r < 20 || from.sll_pkttype == PACKET_OUTGOING)
			continue;
		uint64_t rx = 0;
		for (struct cmsghdr *c = CMSG_FIRSTHDR(&mh); c; c = CMSG_NXTHDR(&mh, c))
			if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_TIMESTAMPNS) {
				struct timespec ts;
				memcpy(&ts, CMSG_DATA(c), sizeof(ts));
				rx = (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
			}
		int off = (buf[0] & 0xf) * 4 + 8 + O.ts_offset;
		if (!rx || r < off + 8)
			continue;
		uint64_t sent;
		memcpy(&sent, buf + off, 8);
		g_tap_msgs++;
		if (rx >= sent)
			hist_add(&g_tap_lat, rx - sent);
		else
			g_tap_neg++;
	}
	write_stats(tap_json);
	tap_json(stdout);
	fflush(stdout);
	return 0;
}

/* ------------------------------------------------------------------------ */

static void usage(void)
{
	fprintf(stderr,
		"usage: rpcbench server|client|pub|sub|tap [options]\n"
		"common:\n"
		"  --proto udp|tcp      transport (default udp; feed modes are UDP only)\n"
		"  --port N             port (default 9000)\n"
		"  --threads N          worker threads (default 1)\n"
		"  --cpus LIST          CPUs to pin threads to, round-robin (default 0)\n"
		"  --req-size B         request / feed message size, >=16 (default 64); alias --size\n"
		"  --resp-size B        response size, >=16 (default 64)\n"
		"server:\n"
		"  --spin               busy-poll instead of blocking in epoll_wait\n"
		"  --cpu-steer          deliver each request to the thread on the CPU that\n"
		"                       processed it in the kernel (reuseport BPF)\n"
		"  --work-lines N       random cache lines touched per request (default 256)\n"
		"  --wss-kb N           per-thread working-set size in KiB (default 512)\n"
		"  --max-seconds S      exit after S seconds (default: run until SIGINT/SIGTERM)\n"
		"client:\n"
		"  --host H             server address (default 127.0.0.1)\n"
		"  --conns N            connections/sockets per thread (default 8)\n"
		"  --rate R             total requests/s, open loop; 0 = closed loop (default 0)\n"
		"  --poisson            exponential inter-arrival times (open loop)\n"
		"  --outstanding N      in-flight requests per connection, closed loop (default 1)\n"
		"  --duration S         measured seconds (default 10)\n"
		"  --warmup S           unmeasured warm-up seconds (default 2)\n"
		"  --timeout-ms N       UDP loss timeout / drain time (default 200)\n"
		"pub (feed publisher):\n"
		"  --host H             destination address or multicast group\n"
		"  --rate R             total messages/s (0 = as fast as possible)\n"
		"  --burst N            messages sent back-to-back per tick (default 1)\n"
		"  --flows N            source sockets/ports, spreads RSS (default 1)\n"
		"  --duration S         seconds to publish (default 10)\n"
		"  --payload FILE       send this file's bytes as every message\n"
		"  --mcast-if ADDR      local address for multicast; --ttl N (default 8)\n"
		"sub (synthetic feed handler) / tap (passive latency probe):\n"
		"  --group G[,G...]     multicast groups to join, one per thread (sub)\n"
		"  --mcast-if ADDR      interface address for the joins (sub)\n"
		"  --spin               busy-poll the socket (sub); --busy-poll US sets SO_BUSY_POLL\n"
		"  --cpu-steer          reuseport steering by receive CPU (sub, unicast)\n"
		"  --work-lines/--wss-kb  per-message work (sub, default 256 / 512)\n"
		"  --iface IF           interface to tap (tap)\n"
		"  --stats-file F       JSON snapshot on SIGUSR2 and exit; SIGUSR1 resets stats\n"
		"feed message layout (pub/sub/tap):\n"
		"  --seq-offset N       per-flow u64 sequence (default 0, -1 = none)\n"
		"  --ts-offset N        u64 CLOCK_REALTIME send ns (default 8, -1 = none)\n"
		"  --flow-offset N      u32 flow id (default 16, -1 = none)\n"
		"  (with --payload the defaults are -1: nothing is overwritten)\n");
	exit(2);
}

int main(int argc, char **argv)
{
	if (argc < 2)
		usage();
	const char *modes[] = {"server", "client", "pub", "sub", "tap"};
	O.mode = -1;
	for (int i = 0; i < 5; i++)
		if (!strcmp(argv[1], modes[i]))
			O.mode = i;
	if (O.mode < 0)
		usage();

	static struct option lo[] = {
		{"proto", 1, 0, 'p'},	    {"port", 1, 0, 'P'},     {"threads", 1, 0, 't'},
		{"cpus", 1, 0, 'c'},	    {"req-size", 1, 0, 'q'}, {"size", 1, 0, 'q'},
		{"resp-size", 1, 0, 'r'},   {"spin", 0, 0, 's'},     {"cpu-steer", 0, 0, 'S'},
		{"work-lines", 1, 0, 'w'},  {"wss-kb", 1, 0, 'W'},   {"max-seconds", 1, 0, 'M'},
		{"host", 1, 0, 'H'},	    {"conns", 1, 0, 'n'},    {"rate", 1, 0, 'R'},
		{"poisson", 0, 0, 'X'},	    {"outstanding", 1, 0, 'o'}, {"duration", 1, 0, 'd'},
		{"warmup", 1, 0, 'u'},	    {"timeout-ms", 1, 0, 'T'}, {"burst", 1, 0, 'B'},
		{"flows", 1, 0, 'F'},	    {"payload", 1, 0, 'L'},  {"mcast-if", 1, 0, 'I'},
		{"ttl", 1, 0, 'Y'},	    {"group", 1, 0, 'G'},    {"busy-poll", 1, 0, 'b'},
		{"iface", 1, 0, 'i'},	    {"stats-file", 1, 0, 'f'}, {"seq-offset", 1, 0, 1},
		{"ts-offset", 1, 0, 2},	    {"flow-offset", 1, 0, 3},
		{0, 0, 0, 0}};
	O.cpus[0] = 0;
	O.ncpus = 1;
	int ch;
	optind = 2;
	while ((ch = getopt_long(argc, argv, "", lo, NULL)) != -1) {
		switch (ch) {
		case 'p': O.tcp = !strcmp(optarg, "tcp"); break;
		case 'P': O.port = atoi(optarg); break;
		case 't': O.nthreads = atoi(optarg); break;
		case 'c': O.ncpus = parse_cpulist(optarg, O.cpus, MAX_CPUS); break;
		case 'q': O.req_size = atoi(optarg); break;
		case 'r': O.resp_size = atoi(optarg); break;
		case 's': O.spin = 1; break;
		case 'S': O.cpu_steer = 1; break;
		case 'w': O.work_lines = atoi(optarg); break;
		case 'W': O.wss_kb = atoi(optarg); break;
		case 'M': O.max_seconds = atof(optarg); break;
		case 'H': snprintf(O.host, sizeof(O.host), "%s", optarg); break;
		case 'n': O.conns = atoi(optarg); break;
		case 'R': O.rate = atof(optarg); break;
		case 'X': O.poisson = 1; break;
		case 'o': O.outstanding = atoi(optarg); break;
		case 'd': O.duration = atof(optarg); break;
		case 'u': O.warmup = atof(optarg); break;
		case 'T': O.timeout_ms = atoi(optarg); break;
		case 'B': O.burst = atoi(optarg); break;
		case 'F': O.flows = atoi(optarg); break;
		case 'L': snprintf(O.payload, sizeof(O.payload), "%s", optarg); break;
		case 'I': snprintf(O.mcast_if, sizeof(O.mcast_if), "%s", optarg); break;
		case 'Y': O.ttl = atoi(optarg); break;
		case 'G': snprintf(O.groups, sizeof(O.groups), "%s", optarg); break;
		case 'b': O.busy_poll = atoi(optarg); break;
		case 'i': snprintf(O.iface, sizeof(O.iface), "%s", optarg); break;
		case 'f': snprintf(O.stats_file, sizeof(O.stats_file), "%s", optarg); break;
		case 1: O.seq_offset = atoi(optarg); break;
		case 2: O.ts_offset = atoi(optarg); break;
		case 3: O.flow_offset = atoi(optarg); break;
		default: usage();
		}
	}
	if (O.req_size < HDR_SIZE || O.req_size > MAX_MSG || O.resp_size < HDR_SIZE ||
	    O.resp_size > MAX_MSG || O.nthreads < 1 || O.ncpus < 1 || O.conns < 1 ||
	    O.outstanding < 1 || O.burst < 1 || O.flows < 1 || O.flows > MAX_FLOWS) {
		fprintf(stderr, "invalid option values\n");
		return 2;
	}
	if (O.mode == 4 && !O.iface[0]) {
		fprintf(stderr, "tap needs --iface\n");
		return 2;
	}

	struct sigaction sa = {.sa_handler = on_signal};
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	struct sigaction s1 = {.sa_handler = on_usr1}, s2 = {.sa_handler = on_usr2};
	sigaction(SIGUSR1, &s1, NULL);
	sigaction(SIGUSR2, &s2, NULL);
	signal(SIGPIPE, SIG_IGN);
	switch (O.mode) {
	case 0: return run_server();
	case 1: return run_client();
	case 2: return run_pub();
	case 3: return run_sub();
	default: return run_tap();
	}
}

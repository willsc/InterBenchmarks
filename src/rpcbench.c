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
#include <math.h>
#include <netdb.h>
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
	int is_server;
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
	int idx, cpu;
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
	int lfd;
	if (O.tcp) {
		lfd = reuseport_socket(SOCK_STREAM);
		if (listen(lfd, 1024) < 0)
			die("listen");
	} else {
		lfd = reuseport_socket(SOCK_DGRAM);
	}
	set_nonblock(lfd);
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
	       "\"work_lines\":%d,\"wss_kb\":%d,",
	       O.tcp ? "tcp" : "udp", O.spin ? "spin" : "block", O.nthreads, O.work_lines,
	       O.wss_kb);
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

static void usage(void)
{
	fprintf(stderr,
		"usage: rpcbench server|client [options]\n"
		"common:\n"
		"  --proto udp|tcp      transport (default udp)\n"
		"  --port N             port (default 9000)\n"
		"  --threads N          worker threads (default 1)\n"
		"  --cpus LIST          CPUs to pin threads to, round-robin (default 0)\n"
		"  --req-size B         request size, >=16 (default 64)\n"
		"  --resp-size B        response size, >=16 (default 64)\n"
		"server:\n"
		"  --spin               busy-poll instead of blocking in epoll_wait\n"
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
		"  --timeout-ms N       UDP loss timeout / drain time (default 200)\n");
	exit(2);
}

int main(int argc, char **argv)
{
	if (argc < 2)
		usage();
	if (!strcmp(argv[1], "server"))
		O.is_server = 1;
	else if (strcmp(argv[1], "client"))
		usage();

	static struct option lo[] = {
		{"proto", 1, 0, 'p'},	    {"port", 1, 0, 'P'},     {"threads", 1, 0, 't'},
		{"cpus", 1, 0, 'c'},	    {"req-size", 1, 0, 'q'}, {"resp-size", 1, 0, 'r'},
		{"spin", 0, 0, 's'},	    {"work-lines", 1, 0, 'w'}, {"wss-kb", 1, 0, 'W'},
		{"max-seconds", 1, 0, 'M'}, {"host", 1, 0, 'H'},     {"conns", 1, 0, 'n'},
		{"rate", 1, 0, 'R'},	    {"poisson", 0, 0, 'X'},  {"outstanding", 1, 0, 'o'},
		{"duration", 1, 0, 'd'},    {"warmup", 1, 0, 'u'},   {"timeout-ms", 1, 0, 'T'},
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
		default: usage();
		}
	}
	if (O.req_size < HDR_SIZE || O.req_size > MAX_MSG || O.resp_size < HDR_SIZE ||
	    O.resp_size > MAX_MSG || O.nthreads < 1 || O.ncpus < 1 || O.conns < 1 ||
	    O.outstanding < 1) {
		fprintf(stderr, "invalid option values\n");
		return 2;
	}

	struct sigaction sa = {.sa_handler = on_signal};
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	signal(SIGPIPE, SIG_IGN);
	return O.is_server ? run_server() : run_client();
}

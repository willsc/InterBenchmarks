CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter
LDLIBS = -lpthread -lm

all: rpcbench

rpcbench: src/rpcbench.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f rpcbench

.PHONY: all clean

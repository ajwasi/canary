/* PC test build only.
 *
 * The Linux port of FreeRTOS drives its tick with a signal a thousand times a second.
 * A signal that arrives while a task waits in read() or write() makes the call fail with
 * EINTR, which the TLS layer reports as a broken connection. On the chip this cannot
 * happen. These wrappers repeat the call instead, as ESP-IDF itself does for select(). */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/types.h>

#define RETRY_ON_EINTR(call)                    \
    ssize_t r;                                  \
    do {                                        \
        r = (call);                             \
    } while (r < 0 && errno == EINTR);          \
    return r

ssize_t read(int fd, void *buf, size_t count)
{
    static ssize_t (*real)(int, void *, size_t);
    if (!real) real = dlsym(RTLD_NEXT, "read");
    RETRY_ON_EINTR(real(fd, buf, count));
}

ssize_t write(int fd, const void *buf, size_t count)
{
    static ssize_t (*real)(int, const void *, size_t);
    if (!real) real = dlsym(RTLD_NEXT, "write");
    RETRY_ON_EINTR(real(fd, buf, count));
}

ssize_t recv(int fd, void *buf, size_t len, int flags)
{
    static ssize_t (*real)(int, void *, size_t, int);
    if (!real) real = dlsym(RTLD_NEXT, "recv");
    RETRY_ON_EINTR(real(fd, buf, len, flags));
}

ssize_t send(int fd, const void *buf, size_t len, int flags)
{
    static ssize_t (*real)(int, const void *, size_t, int);
    if (!real) real = dlsym(RTLD_NEXT, "send");
    RETRY_ON_EINTR(real(fd, buf, len, flags));
}

#include "common.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

bool interface_exists(const char *ifname)
{
    char path[PATH_MAX];
    int n = snprintf(path, sizeof(path), "/sys/class/net/%s", ifname);

    return n > 0 && (size_t)n < sizeof(path) && access(path, F_OK) == 0;
}

int path_join(char *out, size_t out_size, const char *dir, const char *name)
{
    int n = snprintf(out, out_size, "%s/%s", dir, name);

    if (n < 0 || (size_t)n >= out_size) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return 0;
}

int read_text_file(const char *path, char *buf, size_t size)
{
    int fd;
    size_t used = 0;

    if (size == 0) {
        errno = EINVAL;
        return -1;
    }

    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;

    while (used + 1 < size) {
        ssize_t n = read(fd, buf + used, size - used - 1);
        if (n < 0) {
            int saved = errno;
            if (saved == EINTR)
                continue;
            close(fd);
            errno = saved;
            return -1;
        }
        if (n == 0)
            break;
        used += (size_t)n;
    }
    buf[used] = '\0';

    if (close(fd) < 0)
        return -1;
    return 0;
}

static int write_all(int fd, const char *buf, size_t len)
{
    size_t done = 0;

    while (done < len) {
        ssize_t n = write(fd, buf + done, len - done);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (n == 0) {
            errno = EIO;
            return -1;
        }
        done += (size_t)n;
    }
    return 0;
}

int write_text_file(const char *path, const char *text)
{
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    int saved;

    if (fd < 0)
        return -1;
    if (write_all(fd, text, strlen(text)) < 0) {
        saved = errno;
        close(fd);
        errno = saved;
        return -1;
    }
    return close(fd);
}

int make_pidfile_path(const char *ifname, char *out, size_t out_size)
{
    int n = snprintf(out, out_size, "/run/ath9k-edca-%s.pid", ifname);

    if (n < 0 || (size_t)n >= out_size) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return 0;
}

int validate_ipv4_cidr(const char *text)
{
    char copy[64];
    char *slash;
    char *end = NULL;
    long prefix;
    struct in_addr addr;

    if (!text || strlen(text) >= sizeof(copy)) {
        errno = EINVAL;
        return -1;
    }
    strcpy(copy, text);
    slash = strchr(copy, '/');
    if (!slash || slash == copy || slash[1] == '\0') {
        errno = EINVAL;
        return -1;
    }
    *slash++ = '\0';
    if (inet_pton(AF_INET, copy, &addr) != 1) {
        errno = EINVAL;
        return -1;
    }
    errno = 0;
    prefix = strtol(slash, &end, 10);
    if (errno || end == slash || *end != '\0' || prefix < 0 || prefix > 32) {
        errno = EINVAL;
        return -1;
    }
    return 0;
}

void sleep_ms(unsigned int milliseconds)
{
    struct timespec ts;

    ts.tv_sec = milliseconds / 1000U;
    ts.tv_nsec = (long)(milliseconds % 1000U) * 1000000L;
    while (nanosleep(&ts, &ts) < 0 && errno == EINTR)
        ;
}

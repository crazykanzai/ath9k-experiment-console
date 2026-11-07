#include "hostapd_ctrl.h"
#include "common.h"
#include "process.h"

#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int hostapd_parse_config(const char *path, struct hostapd_config_info *info)
{
    FILE *fp;
    char line[512];

    memset(info, 0, sizeof(*info));
    fp = fopen(path, "r");
    if (!fp)
        return -1;

    while (fgets(line, sizeof(line), fp)) {
        char *p = line;
        char *eq;
        char *end;

        while (isspace((unsigned char)*p))
            p++;
        if (*p == '#' || *p == '\0')
            continue;
        eq = strchr(p, '=');
        if (!eq)
            continue;
        *eq++ = '\0';
        end = eq + strlen(eq);
        while (end > eq && isspace((unsigned char)end[-1]))
            *--end = '\0';

        if (strcmp(p, "interface") == 0)
            snprintf(info->interface, sizeof(info->interface), "%s", eq);
        else if (strcmp(p, "ssid") == 0)
            snprintf(info->ssid, sizeof(info->ssid), "%s", eq);
    }
    if (ferror(fp)) {
        int saved = errno ? errno : EIO;
        fclose(fp);
        errno = saved;
        return -1;
    }
    fclose(fp);

    if (!info->interface[0] || !info->ssid[0]) {
        errno = EINVAL;
        return -1;
    }
    return 0;
}

int hostapd_resolve_config(const char *path, char *resolved, size_t size)
{
    char tmp[PATH_MAX];

    if (!realpath(path, tmp))
        return -1;
    if (strlen(tmp) >= size) {
        errno = ENAMETOOLONG;
        return -1;
    }
    strcpy(resolved, tmp);
    return 0;
}

pid_t hostapd_read_pid(const char *pidfile)
{
    char text[64];
    char *end = NULL;
    long value;

    if (read_text_file(pidfile, text, sizeof(text)) < 0)
        return -1;
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno || end == text || value <= 1 || value > INT_MAX)
        return -1;
    return (pid_t)value;
}

bool hostapd_pid_alive(pid_t pid)
{
    char path[64];
    char comm[64];
    size_t len;

    if (pid <= 1 || kill(pid, 0) < 0)
        return false;
    snprintf(path, sizeof(path), "/proc/%ld/comm", (long)pid);
    if (read_text_file(path, comm, sizeof(comm)) < 0)
        return false;
    len = strlen(comm);
    while (len && isspace((unsigned char)comm[len - 1]))
        comm[--len] = '\0';
    return strcmp(comm, "hostapd") == 0;
}

int hostapd_cleanup_stale_pidfile(const char *pidfile)
{
    pid_t pid;

    if (access(pidfile, F_OK) != 0)
        return 0;
    pid = hostapd_read_pid(pidfile);
    if (pid > 1 && hostapd_pid_alive(pid)) {
        errno = EBUSY;
        return -1;
    }
    return unlink(pidfile);
}

int hostapd_start(const char *config, const char *pidfile,
                  char *error_text, size_t error_size)
{
    char *argv[] = { "hostapd", "-B", "-P", (char *)pidfile,
                     (char *)config, NULL };
    int code = 0;

    if (process_run(argv, error_text, error_size, &code) < 0)
        return -1;
    if (code != 0) {
        errno = EIO;
        return -1;
    }
    return 0;
}

int hostapd_stop(const char *pidfile, unsigned int timeout_ms)
{
    pid_t pid = hostapd_read_pid(pidfile);
    unsigned int waited = 0;

    if (pid <= 1) {
        if (access(pidfile, F_OK) == 0)
            (void)unlink(pidfile);
        return 0;
    }
    if (!hostapd_pid_alive(pid)) {
        (void)unlink(pidfile);
        return 0;
    }

    if (kill(pid, SIGTERM) < 0 && errno != ESRCH)
        return -1;
    while (waited < timeout_ms) {
        if (!hostapd_pid_alive(pid)) {
            (void)unlink(pidfile);
            return 0;
        }
        sleep_ms(100);
        waited += 100;
    }

    if (kill(pid, SIGKILL) < 0 && errno != ESRCH)
        return -1;
    sleep_ms(100);
    (void)unlink(pidfile);
    return 0;
}

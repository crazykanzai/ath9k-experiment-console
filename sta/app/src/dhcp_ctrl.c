#include "dhcp_ctrl.h"
#include "process.h"

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int script_path(char *path, size_t size)
{
    char binary[PATH_MAX];
    char *slash;
    ssize_t length = readlink("/proc/self/exe", binary, sizeof(binary) - 1);
    int n;

    if (length < 0)
        return -1;
    binary[length] = '\0';
    slash = strrchr(binary, '/');
    if (!slash) {
        errno = EINVAL;
        return -1;
    }
    *slash = '\0';
    n = snprintf(path, size, "%s/scripts/start_dhcp.sh", binary);
    if (n < 0 || (size_t)n >= size) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return 0;
}

static int run_dhcp(const char *action, const char *ifname,
                    const char *ap_ip_cidr, char *detail, size_t detail_size)
{
    char script[PATH_MAX];
    char *argv[] = { "/bin/bash", script, (char *)action, (char *)ifname,
                     (char *)ap_ip_cidr, NULL };
    int code = 0;

    if (detail && detail_size)
        detail[0] = '\0';
    if (!detail || !detail_size || script_path(script, sizeof(script)) < 0)
        return -1;
    if (access(script, R_OK) < 0) {
        snprintf(detail, detail_size, "DHCP script missing: %s", script);
        return -1;
    }
    if (process_run(argv, detail, detail_size, &code) < 0)
        return -1;
    if (code != 0) {
        errno = EIO;
        return -1;
    }
    return 0;
}

int dhcp_start(const char *ifname, const char *ap_ip_cidr,
               char *detail, size_t detail_size)
{
    return run_dhcp("start", ifname, ap_ip_cidr, detail, detail_size);
}

int dhcp_stop(const char *ifname, char *detail, size_t detail_size)
{
    return run_dhcp("stop", ifname, NULL, detail, detail_size);
}

int dhcp_status(const char *ifname, char *detail, size_t detail_size)
{
    return run_dhcp("status", ifname, NULL, detail, detail_size);
}

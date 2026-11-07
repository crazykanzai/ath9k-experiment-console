#include "wifi_nm.h"
#include "process.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

static int run(char *const argv[], char *error, size_t error_size,
               bool accept_nonzero)
{
    int code = 0;

    if (process_run(argv, error, error_size, &code) < 0)
        return -1;
    if (code != 0 && !accept_nonzero) {
        errno = EIO;
        return -1;
    }
    return code;
}

int wifi_nm_set_managed(const char *ifname, bool managed,
                        char *error, size_t error_size)
{
    char *argv[] = { "nmcli", "dev", "set", (char *)ifname,
                     "managed", managed ? "yes" : "no", NULL };
    return run(argv, error, error_size, false);
}

int wifi_nm_disconnect(const char *ifname, char *error, size_t error_size)
{
    char *argv[] = { "nmcli", "dev", "disconnect", (char *)ifname, NULL };
    return run(argv, error, error_size, true);
}

int wifi_nm_connect(const char *ifname, const char *ssid,
                    const char *password, const char *connection_name,
                    char *error, size_t error_size)
{
    char *argv[14];
    int i = 0;

    argv[i++] = "nmcli";
    argv[i++] = "dev";
    argv[i++] = "wifi";
    argv[i++] = "connect";
    argv[i++] = (char *)ssid;
    if (password && password[0]) {
        argv[i++] = "password";
        argv[i++] = (char *)password;
    }
    argv[i++] = "ifname";
    argv[i++] = (char *)ifname;
    if (connection_name && connection_name[0]) {
        argv[i++] = "name";
        argv[i++] = (char *)connection_name;
    }
    argv[i] = NULL;
    if (run(argv, error, error_size, false) < 0)
        return -1;

    /* Enforce DHCP even when NetworkManager reused an older static profile. */
    return wifi_nm_enable_dhcp(ifname, error, error_size);
}

int wifi_nm_get_active_connection(const char *ifname, char *connection,
                                  size_t connection_size)
{
    char *argv[] = { "nmcli", "-g", "GENERAL.CONNECTION", "device",
                     "show", (char *)ifname, NULL };
    size_t length;

    if (!connection || connection_size == 0) {
        errno = EINVAL;
        return -1;
    }
    if (run(argv, connection, connection_size, false) < 0)
        return -1;
    length = strlen(connection);
    while (length > 0 &&
           (connection[length - 1] == '\n' || connection[length - 1] == '\r'))
        connection[--length] = '\0';
    if (length == 0 || strcmp(connection, "--") == 0) {
        connection[0] = '\0';
        errno = ENOENT;
        return -1;
    }
    return 0;
}

int wifi_nm_enable_dhcp(const char *ifname, char *error, size_t error_size)
{
    char connection[256];
    char *modify_argv[] = {
        "nmcli", "connection", "modify", connection,
        "ipv4.method", "auto",
        "ipv4.addresses", "",
        "ipv4.gateway", "",
        "ipv4.dns", "",
        NULL
    };
    char *up_argv[] = {
        "nmcli", "connection", "up", connection,
        "ifname", (char *)ifname, NULL
    };

    if (wifi_nm_get_active_connection(ifname, connection,
                                      sizeof(connection)) < 0) {
        if (error && error_size > 0)
            snprintf(error, error_size,
                     "no active NetworkManager connection on %s", ifname);
        return -1;
    }
    if (run(modify_argv, error, error_size, false) < 0)
        return -1;
    return run(up_argv, error, error_size, false);
}

int wifi_link_set(const char *ifname, bool up, char *error, size_t error_size)
{
    char *argv[] = { "ip", "link", "set", "dev", (char *)ifname,
                     up ? "up" : "down", NULL };
    return run(argv, error, error_size, false);
}

int wifi_addr_flush(const char *ifname, char *error, size_t error_size)
{
    char *argv[] = { "ip", "addr", "flush", "dev", (char *)ifname, NULL };
    return run(argv, error, error_size, false);
}

int wifi_addr_replace(const char *ifname, const char *cidr,
                      char *error, size_t error_size)
{
    char *argv[] = { "ip", "addr", "replace", (char *)cidr,
                     "dev", (char *)ifname, NULL };
    return run(argv, error, error_size, false);
}

int wifi_iw_set_managed(const char *ifname, char *error, size_t error_size)
{
    char *argv[] = { "iw", "dev", (char *)ifname,
                     "set", "type", "managed", NULL };
    return run(argv, error, error_size, false);
}

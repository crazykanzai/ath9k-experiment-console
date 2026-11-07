#ifndef ATH9K_EDCA_HOSTAPD_CTRL_H
#define ATH9K_EDCA_HOSTAPD_CTRL_H

#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>

struct hostapd_config_info {
    char interface[64];
    char ssid[128];
};

int hostapd_parse_config(const char *path, struct hostapd_config_info *info);
int hostapd_resolve_config(const char *path, char *resolved, size_t size);
pid_t hostapd_read_pid(const char *pidfile);
bool hostapd_pid_alive(pid_t pid);
int hostapd_cleanup_stale_pidfile(const char *pidfile);
int hostapd_start(const char *config, const char *pidfile,
                  char *error_text, size_t error_size);
int hostapd_stop(const char *pidfile, unsigned int timeout_ms);

#endif

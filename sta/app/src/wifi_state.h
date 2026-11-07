#ifndef ATH9K_EDCA_WIFI_STATE_H
#define ATH9K_EDCA_WIFI_STATE_H

#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>

enum wifi_mode_state {
    WIFI_STATE_UNKNOWN = 0,
    WIFI_STATE_STA_IDLE,
    WIFI_STATE_STA_CONNECTED,
    WIFI_STATE_AP_PARTIAL,
    WIFI_STATE_AP_RUNNING
};

struct wifi_state {
    enum wifi_mode_state state;
    char iftype[32];
    char ssid[128];
    char connection[128];
    char ipv4[64];
    int nm_state;
    bool link_up;
    pid_t hostapd_pid;
    bool hostapd_alive;
};

int wifi_state_read(const char *ifname, const char *pidfile,
                    struct wifi_state *state);
const char *wifi_state_name(enum wifi_mode_state state);
void wifi_state_print(const char *ifname, const struct wifi_state *state);
bool wifi_state_is_ap_ready(const struct wifi_state *state,
                            const char *expected_ssid,
                            const char *expected_ip);

#endif

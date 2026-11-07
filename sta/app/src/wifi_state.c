#include "wifi_state.h"
#include "common.h"
#include "hostapd_ctrl.h"
#include "process.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void trim(char *s)
{
    char *p = s;
    size_t len;

    while (isspace((unsigned char)*p))
        p++;
    if (p != s)
        memmove(s, p, strlen(p) + 1);
    len = strlen(s);
    while (len && isspace((unsigned char)s[len - 1]))
        s[--len] = '\0';
}

static void parse_iw(const char *text, struct wifi_state *s)
{
    char copy[OUTPUT_BUF_SIZE];
    char *save = NULL;
    char *line;

    snprintf(copy, sizeof(copy), "%s", text);
    for (line = strtok_r(copy, "\n", &save); line;
         line = strtok_r(NULL, "\n", &save)) {
        trim(line);
        if (strncmp(line, "type ", 5) == 0)
            snprintf(s->iftype, sizeof(s->iftype), "%s", line + 5);
        else if (strncmp(line, "ssid ", 5) == 0)
            snprintf(s->ssid, sizeof(s->ssid), "%s", line + 5);
    }
}

static void parse_ip(const char *text, struct wifi_state *s)
{
    char copy[OUTPUT_BUF_SIZE];
    char *save = NULL;
    char *token;
    int index = 0;

    snprintf(copy, sizeof(copy), "%s", text);
    for (token = strtok_r(copy, " \t\r\n", &save); token;
         token = strtok_r(NULL, " \t\r\n", &save), index++) {
        char addrbuf[64];
        char *slash;
        struct in_addr addr;

        if (index == 1)
            s->link_up = strcmp(token, "UP") == 0;
        snprintf(addrbuf, sizeof(addrbuf), "%s", token);
        slash = strchr(addrbuf, '/');
        if (slash)
            *slash = '\0';
        if (inet_pton(AF_INET, addrbuf, &addr) == 1 && s->ipv4[0] == '\0')
            snprintf(s->ipv4, sizeof(s->ipv4), "%s", token);
    }
}

static void parse_nm(const char *text, struct wifi_state *s)
{
    char copy[OUTPUT_BUF_SIZE];
    char *save = NULL;
    char *line;

    snprintf(copy, sizeof(copy), "%s", text);
    for (line = strtok_r(copy, "\n", &save); line;
         line = strtok_r(NULL, "\n", &save)) {
        if (strncmp(line, "GENERAL.STATE:", 14) == 0)
            s->nm_state = atoi(line + 14);
        else if (strncmp(line, "GENERAL.CONNECTION:", 19) == 0) {
            char *p = line + 19;
            trim(p);
            if (strcmp(p, "--") != 0)
                snprintf(s->connection, sizeof(s->connection), "%s", p);
        }
    }
}

int wifi_state_read(const char *ifname, const char *pidfile,
                    struct wifi_state *s)
{
    char iw[OUTPUT_BUF_SIZE] = {0};
    char ip[OUTPUT_BUF_SIZE] = {0};
    char nm[OUTPUT_BUF_SIZE] = {0};
    int code;
    char *iw_argv[] = { "iw", "dev", (char *)ifname, "info", NULL };
    char *ip_argv[] = { "ip", "-brief", "addr", "show", "dev",
                        (char *)ifname, NULL };
    char *nm_argv[] = { "nmcli", "-t", "-f",
                        "GENERAL.STATE,GENERAL.CONNECTION", "dev", "show",
                        (char *)ifname, NULL };

    memset(s, 0, sizeof(*s));
    s->hostapd_pid = hostapd_read_pid(pidfile);
    s->hostapd_alive = hostapd_pid_alive(s->hostapd_pid);

    if (process_run(iw_argv, iw, sizeof(iw), &code) < 0 || code != 0)
        return -1;
    parse_iw(iw, s);

    if (process_run(ip_argv, ip, sizeof(ip), &code) == 0 && code == 0)
        parse_ip(ip, s);
    if (process_run(nm_argv, nm, sizeof(nm), &code) == 0 && code == 0)
        parse_nm(nm, s);

    if (strcmp(s->iftype, "AP") == 0) {
        if (s->hostapd_alive && s->link_up && s->ipv4[0])
            s->state = WIFI_STATE_AP_RUNNING;
        else
            s->state = WIFI_STATE_AP_PARTIAL;
    } else if (strcmp(s->iftype, "managed") == 0) {
        if (s->ssid[0] && s->nm_state >= 100)
            s->state = WIFI_STATE_STA_CONNECTED;
        else
            s->state = WIFI_STATE_STA_IDLE;
    } else {
        s->state = WIFI_STATE_UNKNOWN;
    }
    return 0;
}

const char *wifi_state_name(enum wifi_mode_state state)
{
    switch (state) {
    case WIFI_STATE_STA_IDLE: return "STA_IDLE";
    case WIFI_STATE_STA_CONNECTED: return "STA_CONNECTED";
    case WIFI_STATE_AP_PARTIAL: return "AP_PARTIAL";
    case WIFI_STATE_AP_RUNNING: return "AP_RUNNING";
    default: return "UNKNOWN";
    }
}

void wifi_state_print(const char *ifname, const struct wifi_state *s)
{
    printf("Interface      : %s\n", ifname);
    printf("State          : %s\n", wifi_state_name(s->state));
    printf("802.11 type    : %s\n", s->iftype[0] ? s->iftype : "unknown");
    printf("SSID           : %s\n", s->ssid[0] ? s->ssid : "--");
    printf("IPv4           : %s\n", s->ipv4[0] ? s->ipv4 : "--");
    printf("Link           : %s\n", s->link_up ? "UP" : "DOWN/no carrier");
    printf("NM state       : %d\n", s->nm_state);
    printf("NM connection  : %s\n", s->connection[0] ? s->connection : "--");
    printf("hostapd        : %s", s->hostapd_alive ? "running" : "not running");
    if (s->hostapd_alive)
        printf(" (pid=%ld)", (long)s->hostapd_pid);
    putchar('\n');
}

bool wifi_state_is_ap_ready(const struct wifi_state *s,
                            const char *expected_ssid,
                            const char *expected_ip)
{
    bool ssid_ok = !expected_ssid || !expected_ssid[0] ||
                   strcmp(s->ssid, expected_ssid) == 0;
    bool ip_ok = !expected_ip || !expected_ip[0] ||
                 strcmp(s->ipv4, expected_ip) == 0;

    return s->state == WIFI_STATE_AP_RUNNING && ssid_ok && ip_ok;
}

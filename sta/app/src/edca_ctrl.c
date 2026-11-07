#include "edca_ctrl.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <unistd.h>

#define EDCA_BUF_SIZE 2048
#define NUM_ACS 4
#define AR_DLCL_IFS_BASE 0x1040U
#define AR_DLCL_IFS_STEP 0x4U

struct ac_info {
    char name[3];
    unsigned int qnum;
    unsigned int aifs;
    unsigned int cwmin;
    unsigned int cwmax;
    bool valid;
};

static const char *const valid_ac_names[NUM_ACS] = {
    "VO", "VI", "BE", "BK"
};

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

static int write_text_file(const char *path, const char *text)
{
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    int saved_errno;

    if (fd < 0)
        return -1;

    if (write_all(fd, text, strlen(text)) < 0) {
        saved_errno = errno;
        close(fd);
        errno = saved_errno;
        return -1;
    }

    if (close(fd) < 0)
        return -1;

    return 0;
}

static int read_text_file(const char *path, char *buf, size_t size)
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
            int saved_errno = errno;
            if (saved_errno == EINTR)
                continue;
            close(fd);
            errno = saved_errno;
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

static int path_join(char *out, size_t out_size,
                     const char *dir, const char *name)
{
    int n = snprintf(out, out_size, "%s/%s", dir, name);

    if (n < 0 || (size_t)n >= out_size) {
        errno = ENAMETOOLONG;
        return -1;
    }

    return 0;
}

int edca_resolve_debug_dir(const char *ifname, char *out, size_t out_size)
{
    char link_path[PATH_MAX];
    char target[PATH_MAX];
    const char *phy_name;
    ssize_t n;
    int len;

    len = snprintf(link_path, sizeof(link_path),
                   "/sys/class/net/%s/phy80211", ifname);
    if (len < 0 || (size_t)len >= sizeof(link_path)) {
        errno = ENAMETOOLONG;
        return -1;
    }

    n = readlink(link_path, target, sizeof(target) - 1);
    if (n < 0)
        return -1;
    target[n] = '\0';

    phy_name = strrchr(target, '/');
    phy_name = phy_name ? phy_name + 1 : target;
    if (strncmp(phy_name, "phy", 3) != 0 || phy_name[3] == '\0') {
        errno = EINVAL;
        return -1;
    }

    len = snprintf(out, out_size,
                   "/sys/kernel/debug/ieee80211/%s/ath9k", phy_name);
    if (len < 0 || (size_t)len >= out_size) {
        errno = ENAMETOOLONG;
        return -1;
    }

    return 0;
}

static bool ac_name_is_valid(const char *name)
{
    size_t i;

    for (i = 0; i < NUM_ACS; i++) {
        if (strcasecmp(name, valid_ac_names[i]) == 0)
            return true;
    }

    return false;
}

static bool cw_is_valid(unsigned int cw)
{
    switch (cw) {
    case 1:
    case 3:
    case 7:
    case 15:
    case 31:
    case 63:
    case 127:
    case 255:
    case 511:
    case 1023:
        return true;
    default:
        return false;
    }
}

static int parse_uint(const char *text, unsigned int *value)
{
    char *end = NULL;
    unsigned long parsed;

    errno = 0;
    parsed = strtoul(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0' || parsed > UINT_MAX) {
        errno = EINVAL;
        return -1;
    }

    *value = (unsigned int)parsed;
    return 0;
}

static int parse_edca_output(const char *text, struct ac_info acs[NUM_ACS])
{
    char copy[EDCA_BUF_SIZE];
    char *saveptr = NULL;
    char *line;
    int count = 0;

    memset(acs, 0, sizeof(struct ac_info) * NUM_ACS);

    if (strlen(text) >= sizeof(copy)) {
        errno = EOVERFLOW;
        return -1;
    }
    strcpy(copy, text);

    for (line = strtok_r(copy, "\n", &saveptr);
         line != NULL;
         line = strtok_r(NULL, "\n", &saveptr)) {
        char name[3] = {0};
        unsigned int qnum, aifs, cwmin, cwmax;
        int i;

        if (sscanf(line,
                   "AC=%2s qnum=%u aifs=%u cwmin=%u cwmax=%u",
                   name, &qnum, &aifs, &cwmin, &cwmax) != 5)
            continue;

        for (i = 0; i < NUM_ACS; i++) {
            if (strcasecmp(name, valid_ac_names[i]) == 0) {
                strcpy(acs[i].name, valid_ac_names[i]);
                acs[i].qnum = qnum;
                acs[i].aifs = aifs;
                acs[i].cwmin = cwmin;
                acs[i].cwmax = cwmax;
                acs[i].valid = true;
                count++;
                break;
            }
        }
    }

    if (count == 0) {
        errno = EPROTO;
        return -1;
    }

    return 0;
}

static int load_edca(const char *debug_dir,
                     struct ac_info acs[NUM_ACS],
                     char *raw_text, size_t raw_size)
{
    char path[PATH_MAX];

    if (path_join(path, sizeof(path), debug_dir, "edca_ctrl") < 0)
        return -1;

    if (read_text_file(path, raw_text, raw_size) < 0)
        return -1;

    return parse_edca_output(raw_text, acs);
}

int edca_show(const char *debug_dir)
{
    struct ac_info acs[NUM_ACS];
    char text[EDCA_BUF_SIZE];

    if (load_edca(debug_dir, acs, text, sizeof(text)) < 0)
        return -1;

    fputs(text, stdout);
    if (text[0] != '\0' && text[strlen(text) - 1] != '\n')
        putchar('\n');

    return 0;
}

int edca_set(const char *debug_dir, const char *ac_name,
                       const char *aifs_text, const char *cwmin_text,
                       const char *cwmax_text)
{
    char path[PATH_MAX];
    char command[64];
    char normalized_ac[3];
    unsigned int aifs, cwmin, cwmax;
    int n;

    if (!ac_name_is_valid(ac_name)) {
        fprintf(stderr, "Invalid AC '%s'; use VO, VI, BE or BK.\n", ac_name);
        errno = EINVAL;
        return -1;
    }

    if (parse_uint(aifs_text, &aifs) < 0 ||
        parse_uint(cwmin_text, &cwmin) < 0 ||
        parse_uint(cwmax_text, &cwmax) < 0) {
        fprintf(stderr, "AIFS/CW values must be unsigned integers.\n");
        errno = EINVAL;
        return -1;
    }

    if (aifs < 1 || aifs > 15) {
        fprintf(stderr, "AIFS must be in the range 1..15.\n");
        errno = EINVAL;
        return -1;
    }

    if (!cw_is_valid(cwmin) || !cw_is_valid(cwmax) || cwmin > cwmax) {
        fprintf(stderr,
                "CW must be one of 1,3,7,15,31,63,127,255,511,1023 "
                "and CWmin <= CWmax.\n");
        errno = EINVAL;
        return -1;
    }

    normalized_ac[0] = (char)toupper((unsigned char)ac_name[0]);
    normalized_ac[1] = (char)toupper((unsigned char)ac_name[1]);
    normalized_ac[2] = '\0';

    n = snprintf(command, sizeof(command), "%s %u %u %u\n",
                 normalized_ac, aifs, cwmin, cwmax);
    if (n < 0 || (size_t)n >= sizeof(command)) {
        errno = EOVERFLOW;
        return -1;
    }

    if (path_join(path, sizeof(path), debug_dir, "edca_ctrl") < 0)
        return -1;

    if (write_text_file(path, command) < 0)
        return -1;

    printf("Write OK: AC=%s AIFS=%u CWmin=%u CWmax=%u\n",
           normalized_ac, aifs, cwmin, cwmax);
    puts("Software readback:");

    return edca_show(debug_dir);
}

static int read_hw_register(const char *debug_dir, unsigned int address,
                            uint32_t *raw)
{
    char regidx_path[PATH_MAX];
    char regval_path[PATH_MAX];
    char address_text[32];
    char value_text[128];
    char previous[32], selected[32];
    char *end = NULL;
    unsigned long value;
    int n, lock_fd, result = -1, saved_errno;

    if (path_join(regidx_path, sizeof(regidx_path), debug_dir, "regidx") < 0 ||
        path_join(regval_path, sizeof(regval_path), debug_dir, "regval") < 0)
        return -1;

    n = snprintf(address_text, sizeof(address_text), "0x%x\n", address);
    if (n < 0 || (size_t)n >= sizeof(address_text)) {
        errno = EOVERFLOW;
        return -1;
    }

    /* Shared with reg_access.py. Lock the entire selection/read/restore
     * transaction, including restore verification, across processes. */
    lock_fd = open("/run/lock/apstaselect-ath9k-regidx.lock",
                   O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (lock_fd < 0)
        return -1;
    while (flock(lock_fd, LOCK_EX) < 0) {
        if (errno == EINTR)
            continue;
        saved_errno = errno;
        close(lock_fd);
        errno = saved_errno;
        return -1;
    }
    if (read_text_file(regidx_path, previous, sizeof(previous)) < 0)
        goto unlock;
    if (write_text_file(regidx_path, address_text) < 0)
        goto restore;
    if (read_text_file(regidx_path, selected, sizeof(selected)) < 0 ||
        strtoul(selected, NULL, 0) != address) {
        errno = EPROTO;
        goto restore;
    }
    if (read_text_file(regval_path, value_text, sizeof(value_text)) < 0)
        goto restore;
    if (read_text_file(regidx_path, selected, sizeof(selected)) < 0 ||
        strtoul(selected, NULL, 0) != address) {
        errno = EPROTO;
        goto restore;
    }

    errno = 0;
    value = strtoul(value_text, &end, 0);
    if (errno != 0 || end == value_text || value > UINT32_MAX) {
        errno = EPROTO;
        goto restore;
    }

    while (*end != '\0' && isspace((unsigned char)*end))
        end++;
    if (*end != '\0') {
        errno = EPROTO;
        goto restore;
    }

    *raw = (uint32_t)value;
    result = 0;
restore:
    saved_errno = errno;
    if (write_text_file(regidx_path, previous) < 0 ||
        read_text_file(regidx_path, selected, sizeof(selected)) < 0 ||
        strcmp(previous, selected) != 0) {
        result = -1;
        saved_errno = EIO;
    }
    errno = saved_errno;
unlock:
    saved_errno = errno;
    if (flock(lock_fd, LOCK_UN) < 0 && result == 0) {
        result = -1;
        saved_errno = errno;
    }
    if (close(lock_fd) < 0 && result == 0) {
        result = -1;
        saved_errno = errno;
    }
    errno = saved_errno;
    return result;
}

static int print_hw_ac(const char *debug_dir, const struct ac_info *ac)
{
    unsigned int address;
    unsigned int hw_aifs, hw_cwmin, hw_cwmax;
    uint32_t raw;

    if (!ac->valid) {
        fprintf(stderr, "AC information is unavailable.\n");
        errno = ENODEV;
        return -1;
    }

    if (ac->qnum > 9) {
        fprintf(stderr, "Unexpected ath9k qnum=%u.\n", ac->qnum);
        errno = ERANGE;
        return -1;
    }

    address = AR_DLCL_IFS_BASE + ac->qnum * AR_DLCL_IFS_STEP;
    if (read_hw_register(debug_dir, address, &raw) < 0)
        return -1;

    hw_cwmin = raw & 0x3ffU;
    hw_cwmax = (raw >> 10) & 0x3ffU;
    hw_aifs = (raw >> 20) & 0xffU;

    printf("AC=%s qnum=%u reg=0x%04x raw=0x%08x "
           "aifs=%u cwmin=%u cwmax=%u\n",
           ac->name, ac->qnum, address, raw,
           hw_aifs, hw_cwmin, hw_cwmax);

    return 0;
}

int edca_hw(const char *debug_dir, const char *selected_ac)
{
    struct ac_info acs[NUM_ACS];
    char text[EDCA_BUF_SIZE];
    int i;

    if (selected_ac != NULL && !ac_name_is_valid(selected_ac)) {
        fprintf(stderr, "Invalid AC '%s'; use VO, VI, BE or BK.\n",
                selected_ac);
        errno = EINVAL;
        return -1;
    }

    if (load_edca(debug_dir, acs, text, sizeof(text)) < 0)
        return -1;

    for (i = 0; i < NUM_ACS; i++) {
        if (selected_ac != NULL &&
            strcasecmp(selected_ac, valid_ac_names[i]) != 0)
            continue;

        if (print_hw_ac(debug_dir, &acs[i]) < 0)
            return -1;
    }

    return 0;
}

#include "prompt.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

static int read_line(char *out, size_t out_size)
{
    size_t len;

    if (!fgets(out, (int)out_size, stdin)) {
        errno = EIO;
        return -1;
    }
    len = strlen(out);
    if (len > 0 && out[len - 1] == '\n')
        out[--len] = '\0';
    if (len > 0 && out[len - 1] == '\r')
        out[--len] = '\0';
    return 0;
}

int prompt_text(const char *label, const char *default_value,
                char *out, size_t out_size)
{
    if (!isatty(STDIN_FILENO)) {
        errno = ENOTTY;
        return -1;
    }
    if (default_value && default_value[0])
        printf("%s [%s]: ", label, default_value);
    else
        printf("%s: ", label);
    fflush(stdout);

    if (read_line(out, out_size) < 0)
        return -1;
    if (out[0] == '\0' && default_value) {
        if (strlen(default_value) >= out_size) {
            errno = ENAMETOOLONG;
            return -1;
        }
        strcpy(out, default_value);
    }
    return 0;
}

int prompt_secret(const char *label, char *out, size_t out_size)
{
    struct termios oldt, newt;
    int rc;

    if (!isatty(STDIN_FILENO)) {
        errno = ENOTTY;
        return -1;
    }
    if (tcgetattr(STDIN_FILENO, &oldt) < 0)
        return -1;
    newt = oldt;
    newt.c_lflag &= (tcflag_t)~ECHO;

    printf("%s: ", label);
    fflush(stdout);
    if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &newt) < 0)
        return -1;
    rc = read_line(out, out_size);
    (void)tcsetattr(STDIN_FILENO, TCSAFLUSH, &oldt);
    fputc('\n', stdout);
    return rc;
}

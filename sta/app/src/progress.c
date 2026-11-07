#include "progress.h"

#include <stdarg.h>
#include <stdio.h>
#include <unistd.h>

static void clear_line(void)
{
    fputs("\r\033[2K", stdout);
}

void progress_init(struct progress *p)
{
    p->interactive = isatty(STDOUT_FILENO) != 0;
    p->active_line = false;
}

void progress_clear(struct progress *p)
{
    if (p->interactive && p->active_line) {
        clear_line();
        fflush(stdout);
    }
    p->active_line = false;
}

void progress_step(struct progress *p, unsigned int current,
                   unsigned int total, const char *fmt, ...)
{
    va_list ap;

    if (p->interactive)
        clear_line();
    fprintf(stdout, "[%u/%u] ", current, total);
    va_start(ap, fmt);
    vfprintf(stdout, fmt, ap);
    va_end(ap);
    if (p->interactive) {
        fflush(stdout);
        p->active_line = true;
    } else {
        fputc('\n', stdout);
    }
}

void progress_note(struct progress *p, const char *fmt, ...)
{
    va_list ap;

    progress_clear(p);
    va_start(ap, fmt);
    vfprintf(stdout, fmt, ap);
    va_end(ap);
    fputc('\n', stdout);
}

void progress_success(struct progress *p, const char *fmt, ...)
{
    va_list ap;

    progress_clear(p);
    fputs("[OK] ", stdout);
    va_start(ap, fmt);
    vfprintf(stdout, fmt, ap);
    va_end(ap);
    fputc('\n', stdout);
}

void progress_failure(struct progress *p, const char *fmt, ...)
{
    va_list ap;

    progress_clear(p);
    fputs("[FAIL] ", stderr);
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
}

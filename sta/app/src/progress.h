#ifndef ATH9K_EDCA_PROGRESS_H
#define ATH9K_EDCA_PROGRESS_H

#include <stdbool.h>

struct progress {
    bool interactive;
    bool active_line;
};

void progress_init(struct progress *p);
void progress_step(struct progress *p, unsigned int current,
                   unsigned int total, const char *fmt, ...);
void progress_note(struct progress *p, const char *fmt, ...);
void progress_success(struct progress *p, const char *fmt, ...);
void progress_failure(struct progress *p, const char *fmt, ...);
void progress_clear(struct progress *p);

#endif

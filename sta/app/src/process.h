#ifndef ATH9K_EDCA_PROCESS_H
#define ATH9K_EDCA_PROCESS_H

#include <stddef.h>

int process_run(char *const argv[], char *output, size_t output_size,
                int *exit_code);
int process_run_ok(char *const argv[], char *output, size_t output_size);

#endif

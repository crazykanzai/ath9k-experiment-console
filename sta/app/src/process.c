#include "process.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int process_run(char *const argv[], char *output, size_t output_size,
                int *exit_code)
{
    int pipefd[2];
    pid_t pid;
    int status;
    size_t used = 0;

    if (!output || output_size == 0) {
        errno = EINVAL;
        return -1;
    }
    output[0] = '\0';

    if (pipe(pipefd) < 0)
        return -1;

    pid = fork();
    if (pid < 0) {
        int saved = errno;
        close(pipefd[0]);
        close(pipefd[1]);
        errno = saved;
        return -1;
    }

    if (pid == 0) {
        close(pipefd[0]);
        if (dup2(pipefd[1], STDOUT_FILENO) < 0 ||
            dup2(pipefd[1], STDERR_FILENO) < 0)
            _exit(126);
        close(pipefd[1]);
        setenv("LC_ALL", "C", 1);
        execvp(argv[0], argv);
        dprintf(STDERR_FILENO, "cannot execute %s: %s\n", argv[0],
                strerror(errno));
        _exit(127);
    }

    close(pipefd[1]);
    while (used + 1 < output_size) {
        ssize_t n = read(pipefd[0], output + used, output_size - used - 1);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            close(pipefd[0]);
            (void)waitpid(pid, NULL, 0);
            return -1;
        }
        if (n == 0)
            break;
        used += (size_t)n;
    }
    output[used] = '\0';
    close(pipefd[0]);

    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR)
            return -1;
    }

    if (exit_code) {
        if (WIFEXITED(status))
            *exit_code = WEXITSTATUS(status);
        else if (WIFSIGNALED(status))
            *exit_code = 128 + WTERMSIG(status);
        else
            *exit_code = 128;
    }
    return 0;
}

int process_run_ok(char *const argv[], char *output, size_t output_size)
{
    int code = 0;

    if (process_run(argv, output, output_size, &code) < 0)
        return -1;
    if (code != 0) {
        errno = EIO;
        return -1;
    }
    return 0;
}

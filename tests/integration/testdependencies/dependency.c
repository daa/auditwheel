#include "dependency.h"
#include <malloc.h>

int dep_run()
{
#if defined(__GLIBC_PREREQ) && __GLIBC_PREREQ(2, 10)
    return malloc_info(0, stdout);
#else
    return 0;
#endif
}

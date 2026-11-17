/*
 * 离线测试用：模拟 libnvidia-ml（NVML）和 libcuda 的最小实现。
 * 从环境变量 MOCK_NV_CONF 指定的文件读取 GPU 列表，每行一张卡，字段以 | 分隔：
 *   bdf|devid_vendor|subsys|name|mem_mib|cores|buswidth|maxw|maxsm|maxmem|cc|links_total|links_active|serial|uuid|remote_bdfs(逗号分隔)
 * 编译：cc -shared -fPIC -o libmocknv.dylib mocknv.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char busIdLegacy[16];
    unsigned int domain, bus, device, pciDeviceId, pciSubSystemId;
    char busId[32];
} nvmlPciInfo_t;

typedef struct { unsigned long long total, free, used; } nvmlMemory_t;

typedef struct {
    char bdf[32];
    unsigned int devid, subsys;
    char name[96];
    unsigned long long mem_mib;
    unsigned int cores, buswidth, maxw, maxsm, maxmem;
    int ccmaj, ccmin, links_total, links_active;
    char serial[32], uuid[96];
    char remote[18][32];
    int nremote;
} gpu_t;

static gpu_t gpus[16];
static int ngpus = -1;
static int conf_ok = 0;

static void load(void) {
    if (ngpus >= 0) return;
    ngpus = 0;
    const char *path = getenv("MOCK_NV_CONF");
    if (!path) return;
    FILE *f = fopen(path, "r");
    if (!f) return;
    conf_ok = 1;
    char line[4096];
    while (fgets(line, sizeof line, f) && ngpus < 16) {
        if (line[0] == '#' || line[0] == '\n') continue;
        line[strcspn(line, "\n")] = 0;
        char *fld[20];
        int nf = 0;
        char *p = line;
        while (nf < 20) {
            fld[nf++] = p;
            char *q = strchr(p, '|');
            if (!q) break;
            *q = 0;
            p = q + 1;
        }
        if (nf < 16) continue;
        gpu_t *g = &gpus[ngpus];
        memset(g, 0, sizeof *g);
        strncpy(g->bdf, fld[0], 31);
        g->devid = (unsigned)strtoul(fld[1], 0, 16);
        g->subsys = (unsigned)strtoul(fld[2], 0, 16);
        strncpy(g->name, fld[3], 95);
        g->mem_mib = strtoull(fld[4], 0, 10);
        g->cores = (unsigned)atoi(fld[5]);
        g->buswidth = (unsigned)atoi(fld[6]);
        g->maxw = (unsigned)atoi(fld[7]);
        g->maxsm = (unsigned)atoi(fld[8]);
        g->maxmem = (unsigned)atoi(fld[9]);
        sscanf(fld[10], "%d.%d", &g->ccmaj, &g->ccmin);
        g->links_total = atoi(fld[11]);
        g->links_active = atoi(fld[12]);
        strncpy(g->serial, fld[13], 31);
        strncpy(g->uuid, fld[14], 95);
        char *r = fld[15];
        while (r && *r && g->nremote < 18) {
            char *c = strchr(r, ',');
            if (c) *c = 0;
            strncpy(g->remote[g->nremote++], r, 31);
            r = c ? c + 1 : NULL;
        }
        ngpus++;
    }
    fclose(f);
}

static void fill_pci(nvmlPciInfo_t *p, const char *bdf, unsigned devid, unsigned subsys) {
    unsigned d = 0, b = 0, dv = 0, fn = 0;
    memset(p, 0, sizeof *p);
    sscanf(bdf, "%x:%x:%x.%x", &d, &b, &dv, &fn);
    p->domain = d; p->bus = b; p->device = dv;
    p->pciDeviceId = devid; p->pciSubSystemId = subsys;
    snprintf(p->busId, sizeof p->busId, "%08X:%02X:%02X.%X", d, b, dv, fn);
    snprintf(p->busIdLegacy, sizeof p->busIdLegacy, "%04X:%02X:%02X.%X", d, b, dv, fn);
}

/* ---------------- NVML ---------------- */
int nvmlInit_v2(void) { load(); return conf_ok ? 0 : 9; /* NVML_ERROR_DRIVER_NOT_LOADED */ }
int nvmlShutdown(void) { return 0; }
int nvmlSystemGetDriverVersion(char *v, unsigned int len) {
    const char *e = getenv("MOCK_NV_DRIVER");
    snprintf(v, len, "%s", e ? e : "550.54.15");
    return 0;
}
int nvmlDeviceGetCount_v2(unsigned int *n) { load(); *n = (unsigned)ngpus; return 0; }
int nvmlDeviceGetHandleByIndex_v2(unsigned int i, void **h) {
    if ((int)i >= ngpus) return 2;
    *h = &gpus[i];
    return 0;
}
int nvmlDeviceGetName(void *h, char *s, unsigned int len) { snprintf(s, len, "%s", ((gpu_t *)h)->name); return 0; }
int nvmlDeviceGetSerial(void *h, char *s, unsigned int len) { snprintf(s, len, "%s", ((gpu_t *)h)->serial); return 0; }
int nvmlDeviceGetUUID(void *h, char *s, unsigned int len) { snprintf(s, len, "%s", ((gpu_t *)h)->uuid); return 0; }
int nvmlDeviceGetPciInfo_v3(void *h, nvmlPciInfo_t *p) {
    gpu_t *g = h;
    fill_pci(p, g->bdf, g->devid, g->subsys);
    return 0;
}
int nvmlDeviceGetMemoryInfo(void *h, nvmlMemory_t *m) {
    gpu_t *g = h;
    m->total = g->mem_mib * 1024ULL * 1024ULL;
    m->used = 0;
    m->free = m->total;
    return 0;
}
int nvmlDeviceGetNumGpuCores(void *h, unsigned int *c) {
    gpu_t *g = h;
    if (!g->cores) return 3; /* NOT_SUPPORTED */
    *c = g->cores;
    return 0;
}
int nvmlDeviceGetMemoryBusWidth(void *h, unsigned int *w) { *w = ((gpu_t *)h)->buswidth; return 0; }
int nvmlDeviceGetPowerManagementLimitConstraints(void *h, unsigned int *mn, unsigned int *mx) {
    *mn = 200000;
    *mx = ((gpu_t *)h)->maxw * 1000;
    return 0;
}
int nvmlDeviceGetMaxClockInfo(void *h, int type, unsigned int *clk) {
    gpu_t *g = h;
    *clk = (type == 2) ? g->maxmem : g->maxsm;
    return 0;
}
int nvmlDeviceGetCudaComputeCapability(void *h, int *ma, int *mi) {
    gpu_t *g = h;
    *ma = g->ccmaj;
    *mi = g->ccmin;
    return 0;
}
int nvmlDeviceGetNvLinkState(void *h, unsigned int link, int *active) {
    gpu_t *g = h;
    if ((int)link >= g->links_total) return 2;
    *active = (int)link < g->links_active ? 1 : 0;
    return 0;
}
int nvmlDeviceGetNvLinkRemoteDeviceType(void *h, unsigned int link, int *t) { (void)h; (void)link; *t = 2; return 0; }
int nvmlDeviceGetNvLinkRemotePciInfo_v2(void *h, unsigned int link, nvmlPciInfo_t *p) {
    gpu_t *g = h;
    if ((int)link >= g->nremote) return 3;
    fill_pci(p, g->remote[link], 0, 0);
    return 0;
}

/* ---------------- CUDA driver API ---------------- */
int cuInit(unsigned int flags) { (void)flags; load(); return ngpus > 0 ? 0 : 100; }
int cuDeviceGetCount(int *n) { *n = ngpus; return 0; }
int cuDeviceGet(int *d, int i) { if (i >= ngpus) return 101; *d = i; return 0; }
int cuDeviceGetName(char *s, int len, int d) { snprintf(s, (size_t)len, "%s", gpus[d].name); return 0; }
int cuDeviceGetAttribute(int *v, int attr, int d) {
    gpu_t *g = &gpus[d];
    switch (attr) {
        case 16: {                                    /* MULTIPROCESSOR_COUNT：GP100/Volta/Turing/GA100 每 SM 64 核 */
            int c64 = (g->ccmaj == 6 && g->ccmin == 0) || g->ccmaj == 7 || (g->ccmaj == 8 && g->ccmin == 0);
            *v = (int)(g->cores / (c64 ? 64 : 128));
            break;
        }
        case 37: *v = (int)g->buswidth; break;        /* GLOBAL_MEMORY_BUS_WIDTH */
        case 38: *v = 50 * 1024 * 1024; break;        /* L2_CACHE_SIZE */
        case 75: *v = g->ccmaj; break;
        case 76: *v = g->ccmin; break;
        default: *v = 0;
    }
    return 0;
}
int cuDeviceGetPCIBusId(char *s, int len, int d) { snprintf(s, (size_t)len, "%s", gpus[d].bdf); return 0; }

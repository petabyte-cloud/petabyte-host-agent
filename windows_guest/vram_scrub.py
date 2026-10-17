"""vram_scrub.py — zero every byte of device memory the CUDA driver will hand us, then verify.

CUDA driver API via ctypes (nvcuda.dll / libcuda.so.1). Allocates free memory in chunks (backing
off on OOM), cuMemsetD8 to 0, sample-verifies each chunk by copying its head/middle/tail back,
frees, destroys the context. Never raises: anything unexpected -> {"verified": False}.
ponytail: covers what the allocator exposes, not reserved/carved-out VRAM; the host's VFIO reset
remains the hard boundary."""
import ctypes
import os

SAMPLE = 1 << 20          # bytes verified at head/middle/tail of each chunk
MIN_CHUNK = 16 << 20
RESERVE = 32 << 20        # left to the driver so the context itself stays alive
CUDA_SUCCESS, CUDA_ERROR_OUT_OF_MEMORY = 0, 2


def scrub(max_chunk=256 << 20) -> dict:
    try:
        return _scrub(max_chunk)
    except Exception:            # noqa: BLE001 — the scrub must never take the job down
        return {"verified": False, "bytes": 0}


def _load():
    if os.name == "nt":
        return ctypes.WinDLL("nvcuda.dll")
    return ctypes.CDLL("libcuda.so.1")


def _scrub(max_chunk):
    cu = _load()
    ptr_t, size_t = ctypes.c_ulonglong, ctypes.c_size_t
    for name, args in (("cuInit", [ctypes.c_uint]), ("cuDeviceGetCount", [ctypes.POINTER(ctypes.c_int)]),
                       ("cuDeviceGet", [ctypes.POINTER(ctypes.c_int), ctypes.c_int]),
                       ("cuCtxCreate_v2", [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_int]),
                       ("cuCtxDestroy_v2", [ctypes.c_void_p]), ("cuCtxSynchronize", []),
                       ("cuMemGetInfo_v2", [ctypes.POINTER(size_t), ctypes.POINTER(size_t)]),
                       ("cuMemAlloc_v2", [ctypes.POINTER(ptr_t), size_t]), ("cuMemFree_v2", [ptr_t]),
                       ("cuMemsetD8_v2", [ptr_t, ctypes.c_ubyte, size_t]),
                       ("cuMemcpyDtoH_v2", [ctypes.c_void_p, ptr_t, size_t])):
        fn = getattr(cu, name)
        fn.argtypes, fn.restype = args, ctypes.c_int
    if cu.cuInit(0) != CUDA_SUCCESS:
        return {"verified": False, "bytes": 0}
    count = ctypes.c_int(0)
    if cu.cuDeviceGetCount(ctypes.byref(count)) != CUDA_SUCCESS or count.value < 1:
        return {"verified": False, "bytes": 0}
    total, ok = 0, True
    for ordinal in range(count.value):
        n, good = _scrub_device(cu, ordinal, max_chunk, ptr_t, size_t)
        total += n
        ok = ok and good
    return {"verified": ok and total > 0, "bytes": total}


def _scrub_device(cu, ordinal, chunk, ptr_t, size_t):
    dev, ctx = ctypes.c_int(0), ctypes.c_void_p()
    if cu.cuDeviceGet(ctypes.byref(dev), ordinal) != CUDA_SUCCESS:
        return 0, False
    if cu.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev) != CUDA_SUCCESS:
        return 0, False
    held, zeroed, good = [], 0, True
    try:
        free, tot = size_t(0), size_t(0)
        while chunk >= MIN_CHUNK:
            if cu.cuMemGetInfo_v2(ctypes.byref(free), ctypes.byref(tot)) != CUDA_SUCCESS:
                good = False
                break
            want = min(chunk, free.value - RESERVE)
            if want < MIN_CHUNK:
                break
            p = ptr_t(0)
            rc = cu.cuMemAlloc_v2(ctypes.byref(p), want)
            if rc == CUDA_ERROR_OUT_OF_MEMORY:
                chunk //= 2
                continue
            if rc != CUDA_SUCCESS:
                good = False
                break
            held.append((p.value, want))
            if cu.cuMemsetD8_v2(p.value, 0, want) != CUDA_SUCCESS:
                good = False
                break
            zeroed += want
        if cu.cuCtxSynchronize() != CUDA_SUCCESS:
            good = False
        buf = ctypes.create_string_buffer(SAMPLE)
        zero = bytes(SAMPLE)
        for p, n in held if good else []:
            for off in sorted({0, max(0, n // 2 - SAMPLE // 2), max(0, n - SAMPLE)}):
                ln = min(SAMPLE, n - off)
                if cu.cuMemcpyDtoH_v2(buf, p + off, ln) != CUDA_SUCCESS or buf.raw[:ln] != zero[:ln]:
                    good = False
                    break
            if not good:
                break
    finally:
        for p, _ in held:
            cu.cuMemFree_v2(p)
        cu.cuCtxDestroy_v2(ctx)
    return (zeroed if good else 0), good

# HTTP path memory probe

Run against the real server process on loopback with a single worker, so no fork pool and no
scheduler confuse the reading. One write and one read per iteration.

```
batch   0  requests    800  private    111.6 MB
batch   1  requests   1600  private    112.2 MB    +0.58 MB
batch   2  requests   2400  private    112.6 MB    +0.43 MB
batch   3  requests   3200  private    113.0 MB    +0.32 MB
batch   4  requests   4000  private    113.3 MB    +0.34 MB
batch   5  requests   4800  private    113.5 MB    +0.21 MB
batch   6  requests   5600  private    113.6 MB    +0.14 MB
batch   7  requests   6400  private    113.6 MB    +0.00 MB
batch   8  requests   7200  private    113.6 MB    +0.00 MB
batch   9  requests   8000  private    113.6 MB    +0.00 MB
batch  10  requests   8800  private    113.6 MB    +0.00 MB
batch  11  requests   9600  private    113.6 MB    +0.00 MB
batch  12  requests  10400  private    113.7 MB    +0.01 MB
batch  13  requests  11200  private    113.7 MB    +0.08 MB

after warm-up: 112.2 MB -> 113.7 MB over 10400 requests, ratio 1.0137
first half +1.45 MB, second half +0.09 MB
per request over the second half: 20 bytes
```

The plateau is flat for five consecutive batches and the second half grows sixteen times less than
the first. Warm-up ends near 6,000 requests; nothing accumulates after it.

`tests/e2e/test_http_path_memory.py` is this probe, sized so the whole second half falls past that
warm-up point — a shorter run measures warm-up and misreports it as retention, which is what the
first attempt at the test did.

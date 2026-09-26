"""The miner's workers live for the whole run: a new block is a new job handed to the same processes,
never a kill and re-fork of all of them (2026-09-26)."""
import multiprocessing as mp, time
from donutcoin.miner import worker
from donutcoin.crypto import new_keypair
from donutcoin.tx import coinbase_tx


def template(height, prev):
    # bits 0x2100ffff is an enormous target: every hash is a solution, so the test takes milliseconds
    return {"height": height, "prev_hash": prev, "timestamp": int(time.time()), "bits": 0x2100FFFF,
            "txs": [coinbase_tx(height, new_keypair()[2], 1).to_dict()]}


def results(found, n, timeout=20):
    got, end = [], time.time() + timeout
    while len(got) < n and time.time() < end:
        try: got.append(found.get(timeout=0.5))
        except Exception: pass
    return got


def test_workers_take_a_new_job_without_restarting():
    ctx = mp.get_context()
    stop, job_id, counter, found = ctx.Event(), ctx.Value("q", -1), ctx.Value("q", 0), ctx.Queue()
    queues = [ctx.Queue() for _ in range(2)]
    procs = [ctx.Process(target=worker, args=(i, "t", queues[i], job_id, stop, counter, found), daemon=True) for i in range(2)]
    for p in procs: p.start()
    pids = [p.pid for p in procs]
    try:
        for q in queues: q.put((0, template(1, "00" * 32)))
        job_id.value = 0
        first = results(found, 2)
        assert sorted(j for j, _ in first) == [0, 0]            # each worker solves job 0 once, then waits

        for q in queues: q.put((1, template(2, "11" * 32)))
        job_id.value = 1
        second = results(found, 2)
        assert sorted(j for j, _ in second) == [1, 1]
        assert all(b["prev_hash"] == "11" * 32 for _, b in second)
        assert [p.pid for p in procs] == pids and all(p.is_alive() for p in procs)   # the same processes, still running
        tags = {b["txs"][0]["extra"] for _, b in first + second}
        assert tags == {f"t/{i}/{pid}" for i, pid in enumerate(pids)}                   # and they signed both jobs
    finally:
        stop.set()
        for p in procs: p.join(timeout=3)

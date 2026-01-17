#!/usr/bin/env python3
"""
STRESS TEST för shared_lock.py 😈

Testar att låsmekanismen håller under hård belastning med:
- Multipla processer som försöker skriva samtidigt
- Readers som läser medan writers väntar
- Timeout-hantering
- Race conditions
- Deadlock-detektion

Kör: python tools/test_shared_lock_stress.py
"""

import multiprocessing
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.utils.shared_lock import resource_lock, is_locked, clear_stale_locks

# Test-fil för att verifiera att skrivningar inte korrumperas
TEST_FILE = "/tmp/mymemory_stress_test.txt"
ITERATIONS_PER_WORKER = 50
NUM_WRITERS = 8
NUM_READERS = 12
CHAOS_WORKERS = 5


def log(msg: str):
    """Trådsäker loggning."""
    pid = os.getpid()
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] PID-{pid}: {msg}")


def writer_worker(worker_id: int) -> dict:
    """
    Skriver till testfilen med exklusivt lås.
    Verifierar att ingen annan skrev mitt i vår operation.
    """
    stats = {"writes": 0, "conflicts": 0, "timeouts": 0}

    for i in range(ITERATIONS_PER_WORKER):
        try:
            with resource_lock("stress_test", exclusive=True, timeout=10.0):
                # Läs nuvarande värde
                current = 0
                if os.path.exists(TEST_FILE):
                    with open(TEST_FILE, "r") as f:
                        try:
                            current = int(f.read().strip())
                        except ValueError:
                            stats["conflicts"] += 1
                            log(f"😈 KORRUPT DATA UPPTÄCKT av writer-{worker_id}!")
                            continue

                # Simulera arbete (gör det lättare för race conditions att uppstå)
                time.sleep(random.uniform(0.001, 0.01))

                # Skriv nytt värde
                new_value = current + 1
                with open(TEST_FILE, "w") as f:
                    f.write(str(new_value))

                stats["writes"] += 1

                # Verifiera direkt efter skrivning
                with open(TEST_FILE, "r") as f:
                    verify = int(f.read().strip())
                    if verify != new_value:
                        stats["conflicts"] += 1
                        log(f"😈 RACE CONDITION! Skrev {new_value}, läste {verify}")

        except TimeoutError:
            stats["timeouts"] += 1
            log(f"⏰ Writer-{worker_id} timeout på iteration {i}")

    return {"worker_id": worker_id, "type": "writer", **stats}


def reader_worker(worker_id: int) -> dict:
    """
    Läser från testfilen med delat lås.
    Verifierar att värdet är konsistent under läsningen.
    """
    stats = {"reads": 0, "inconsistent": 0, "timeouts": 0}

    for i in range(ITERATIONS_PER_WORKER):
        try:
            with resource_lock("stress_test", exclusive=False, timeout=5.0):
                if os.path.exists(TEST_FILE):
                    # Läs två gånger för att verifiera konsistens
                    with open(TEST_FILE, "r") as f:
                        first_read = f.read().strip()

                    time.sleep(random.uniform(0.001, 0.005))

                    with open(TEST_FILE, "r") as f:
                        second_read = f.read().strip()

                    if first_read != second_read:
                        stats["inconsistent"] += 1
                        log(f"😈 INKONSISTENT LÄSNING! {first_read} -> {second_read}")
                    else:
                        stats["reads"] += 1

        except TimeoutError:
            stats["timeouts"] += 1

    return {"worker_id": worker_id, "type": "reader", **stats}


def chaos_monkey(worker_id: int) -> dict:
    """
    Gör kaotiska saker för att stressa systemet:
    - Tar lås och håller dem länge
    - Släpper lås mitt i operationer
    - Försöker ta nested lås
    """
    stats = {"chaos_ops": 0, "errors": 0}

    for i in range(ITERATIONS_PER_WORKER // 2):
        chaos_type = random.choice(["long_hold", "quick_toggle", "nested", "check_locked"])

        try:
            if chaos_type == "long_hold":
                # Håll låset längre än normalt
                with resource_lock("stress_test", exclusive=True, timeout=15.0):
                    time.sleep(random.uniform(0.05, 0.1))
                    stats["chaos_ops"] += 1

            elif chaos_type == "quick_toggle":
                # Snabba lås/unlock cykler
                for _ in range(10):
                    with resource_lock("stress_test", exclusive=random.choice([True, False]), timeout=2.0):
                        pass
                stats["chaos_ops"] += 1

            elif chaos_type == "nested":
                # Försök ta lås på olika resurser (ska fungera)
                with resource_lock("stress_test", exclusive=True, timeout=5.0):
                    with resource_lock("chaos_resource", exclusive=True, timeout=5.0):
                        time.sleep(0.01)
                        stats["chaos_ops"] += 1

            elif chaos_type == "check_locked":
                # Kolla status utan att ta lås
                _ = is_locked("stress_test")
                stats["chaos_ops"] += 1

        except TimeoutError:
            pass  # Förväntat under stress
        except Exception as e:
            stats["errors"] += 1
            log(f"😈 Chaos error: {e}")

    return {"worker_id": worker_id, "type": "chaos", **stats}


def concurrent_increment_test() -> bool:
    """
    Det ultimata testet: Många processer inkrementerar samma räknare.
    Om låsningen fungerar ska slutvärdet vara exakt summan av alla inkrement.
    """
    log("=" * 60)
    log("🔥 CONCURRENT INCREMENT TEST 🔥")
    log("=" * 60)

    # Reset
    if os.path.exists(TEST_FILE):
        os.remove(TEST_FILE)
    with open(TEST_FILE, "w") as f:
        f.write("0")

    expected_final = NUM_WRITERS * ITERATIONS_PER_WORKER

    log(f"Startar {NUM_WRITERS} writers, {NUM_READERS} readers, {CHAOS_WORKERS} chaos monkeys")
    log(f"Varje worker kör {ITERATIONS_PER_WORKER} iterationer")
    log(f"Förväntat slutvärde: {expected_final}")
    log("-" * 60)

    start_time = time.time()

    with ProcessPoolExecutor(max_workers=NUM_WRITERS + NUM_READERS + CHAOS_WORKERS) as executor:
        futures = []

        # Starta writers
        for i in range(NUM_WRITERS):
            futures.append(executor.submit(writer_worker, i))

        # Starta readers
        for i in range(NUM_READERS):
            futures.append(executor.submit(reader_worker, i))

        # Starta chaos monkeys
        for i in range(CHAOS_WORKERS):
            futures.append(executor.submit(chaos_monkey, i))

        # Samla resultat
        results = []
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                log(f"❌ Worker kraschade: {e}")

    elapsed = time.time() - start_time

    # Analysera resultat
    log("-" * 60)
    log("📊 RESULTAT:")

    total_writes = sum(r["writes"] for r in results if r["type"] == "writer")
    total_reads = sum(r["reads"] for r in results if r["type"] == "reader")
    total_conflicts = sum(r.get("conflicts", 0) for r in results)
    total_inconsistent = sum(r.get("inconsistent", 0) for r in results)
    total_timeouts = sum(r.get("timeouts", 0) for r in results)
    total_chaos = sum(r.get("chaos_ops", 0) for r in results if r["type"] == "chaos")
    total_errors = sum(r.get("errors", 0) for r in results)

    # Läs slutvärdet
    with open(TEST_FILE, "r") as f:
        final_value = int(f.read().strip())

    log(f"  Totala skrivningar: {total_writes}")
    log(f"  Totala läsningar: {total_reads}")
    log(f"  Chaos-operationer: {total_chaos}")
    log(f"  Konflikter upptäckta: {total_conflicts}")
    log(f"  Inkonsistenta läsningar: {total_inconsistent}")
    log(f"  Timeouts: {total_timeouts}")
    log(f"  Errors: {total_errors}")
    log(f"  Tid: {elapsed:.2f}s")
    log("-" * 60)
    log(f"  Förväntat slutvärde: {expected_final}")
    log(f"  Faktiskt slutvärde:  {final_value}")

    success = (final_value == expected_final and total_conflicts == 0 and total_inconsistent == 0)

    if success:
        log("=" * 60)
        log("✅ ALLA TESTER GODKÄNDA! Låsningen håller! 🎉")
        log("=" * 60)
    else:
        log("=" * 60)
        log("❌ TEST MISSLYCKADES! 😈")
        if final_value != expected_final:
            log(f"   Förlorade {expected_final - final_value} skrivningar!")
        if total_conflicts > 0:
            log(f"   {total_conflicts} datakorruptioner!")
        if total_inconsistent > 0:
            log(f"   {total_inconsistent} inkonsistenta läsningar!")
        log("=" * 60)

    return success


def deadlock_test() -> bool:
    """
    Testar att vi inte får deadlocks med nested lås på olika resurser.
    """
    log("\n" + "=" * 60)
    log("🔒 DEADLOCK TEST 🔒")
    log("=" * 60)

    def worker_a():
        for _ in range(20):
            with resource_lock("resource_a", exclusive=True, timeout=2.0):
                time.sleep(0.01)
                with resource_lock("resource_b", exclusive=True, timeout=2.0):
                    time.sleep(0.01)
        return "A done"

    def worker_b():
        for _ in range(20):
            with resource_lock("resource_b", exclusive=True, timeout=2.0):
                time.sleep(0.01)
                with resource_lock("resource_a", exclusive=True, timeout=2.0):
                    time.sleep(0.01)
        return "B done"

    start = time.time()
    timeout_count = 0

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(worker_a),
            executor.submit(worker_b),
            executor.submit(worker_a),
            executor.submit(worker_b),
        ]

        for future in as_completed(futures, timeout=30):
            try:
                future.result()
            except TimeoutError:
                timeout_count += 1
            except Exception as e:
                log(f"  Deadlock test error: {e}")

    elapsed = time.time() - start

    # Om vi kom hit utan att hänga i 30 sekunder är testet godkänt
    # Några timeouts är OK - det visar att timeout-mekanismen fungerar
    success = elapsed < 25  # Borde inte ta mer än 25 sekunder

    if success:
        log(f"✅ Deadlock test godkänt ({elapsed:.2f}s, {timeout_count} timeouts)")
    else:
        log(f"❌ Deadlock test misslyckades - tog för lång tid ({elapsed:.2f}s)")

    return success


def main():
    log("😈😈😈 SHARED LOCK STRESS TEST 😈😈😈")
    log(f"PID: {os.getpid()}")

    # Städa upp gamla lås
    clear_stale_locks()

    # Kör testerna
    test1_ok = concurrent_increment_test()
    test2_ok = deadlock_test()

    # Städa upp
    clear_stale_locks()
    if os.path.exists(TEST_FILE):
        os.remove(TEST_FILE)

    log("\n" + "=" * 60)
    if test1_ok and test2_ok:
        log("🏆 ALLA STRESSTESTER GODKÄNDA! 🏆")
        sys.exit(0)
    else:
        log("💀 NÅGRA TESTER MISSLYCKADES 💀")
        sys.exit(1)


if __name__ == "__main__":
    main()

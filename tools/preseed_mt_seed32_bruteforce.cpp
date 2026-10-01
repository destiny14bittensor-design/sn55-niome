// Exhaustive uint32 initializer search for the observed NumPy RandomState layout.
//
// This models the exact legacy MT19937 raw stream and NumPy rk_interval mask
// rejection used by randint(100, 1000).  Candidates must match the public
// restart prelude, then the first public shuffle and Discovery triplet.
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <mutex>
#include <random>
#include <string>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

static uint32_t interval(std::mt19937& engine, uint32_t maximum) {
    uint32_t mask = maximum;
    mask |= mask >> 1;
    mask |= mask >> 2;
    mask |= mask >> 4;
    mask |= mask >> 8;
    mask |= mask >> 16;
    while (true) {
        const uint32_t value = engine() & mask;
        if (value <= maximum) return value;
    }
}

static bool triplet(std::mt19937& engine, const std::array<uint32_t, 3>& expected) {
    std::array<uint32_t, 3> actual{};
    std::size_t count = 0;
    while (count < actual.size()) {
        const uint32_t value = 100U + interval(engine, 899U);
        bool duplicate = false;
        for (std::size_t index = 0; index < count; ++index) {
            duplicate = duplicate || actual[index] == value;
        }
        if (!duplicate) actual[count++] = value;
    }
    return actual == expected;
}

static void shuffle_256(std::mt19937& engine) {
    // Values themselves are irrelevant; RandomState.shuffle consumes one
    // rk_interval draw for every descending Fisher--Yates index.
    for (uint32_t maximum = 255; maximum > 0; --maximum) {
        (void)interval(engine, maximum);
    }
}

int main(int argc, char** argv) {
    if (argc == 3 && std::string(argv[1]) == "--emit") {
        std::mt19937 engine(static_cast<uint32_t>(std::stoull(argv[2])));
        auto emit = [&](const std::array<uint32_t, 3>& expected) {
            (void)expected;
            std::array<uint32_t, 3> values{};
            std::size_t count = 0;
            while (count < values.size()) {
                const uint32_t value = 100U + interval(engine, 899U);
                if (std::find(values.begin(), values.begin() + count, value) == values.begin() + count) {
                    values[count++] = value;
                }
            }
            std::cout << '[' << values[0] << ',' << values[1] << ',' << values[2] << ']';
        };
        emit({});
        shuffle_256(engine);
        std::cout << ' ';
        emit({});
        std::cout << '\n';
        return 0;
    }
    if (argc < 3 || argc > 4) {
        std::cerr << "usage: preseed_mt_seed32_bruteforce START STOP_EXCLUSIVE [THREADS]\n";
        return 2;
    }
    const uint64_t start = std::stoull(argv[1]);
    const uint64_t stop = std::stoull(argv[2]);
    if (start > stop || stop > (uint64_t{1} << 32)) {
        std::cerr << "range must satisfy 0 <= START <= STOP <= 2^32\n";
        return 2;
    }
    const int threads = argc == 4 ? std::max(1, std::stoi(argv[3])) : 1;
#ifdef _OPENMP
    omp_set_num_threads(threads);
#endif
    constexpr std::array<uint32_t, 3> prelude{654, 347, 964};
    constexpr std::array<uint32_t, 3> first_discovery{491, 210, 379};
    std::atomic<uint64_t> prelude_hits{0};
    std::mutex hits_mutex;
    std::vector<uint32_t> exact_hits;

#pragma omp parallel
    {
        std::vector<uint32_t> local_hits;
#pragma omp for schedule(static)
        for (uint64_t raw_seed = start; raw_seed < stop; ++raw_seed) {
            std::mt19937 engine(static_cast<uint32_t>(raw_seed));
            if (!triplet(engine, prelude)) continue;
            prelude_hits.fetch_add(1, std::memory_order_relaxed);
            shuffle_256(engine);
            if (triplet(engine, first_discovery)) {
                local_hits.push_back(static_cast<uint32_t>(raw_seed));
            }
        }
        if (!local_hits.empty()) {
            std::lock_guard<std::mutex> lock(hits_mutex);
            exact_hits.insert(exact_hits.end(), local_hits.begin(), local_hits.end());
        }
    }
    std::sort(exact_hits.begin(), exact_hits.end());
    std::cout << "{\"start\":" << start << ",\"stop_exclusive\":" << stop
              << ",\"tested\":" << (stop - start)
              << ",\"prelude_hits\":" << prelude_hits.load()
              << ",\"first_discovery_hits\":[";
    for (std::size_t index = 0; index < exact_hits.size(); ++index) {
        if (index) std::cout << ',';
        std::cout << exact_hits[index];
    }
    std::cout << "]}\n";
    return 0;
}

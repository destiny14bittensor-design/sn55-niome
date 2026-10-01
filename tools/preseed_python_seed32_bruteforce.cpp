// Exhaustive uint32 seed search for CPython random.Random / module-global MT19937.
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <iostream>
#include <mutex>
#include <string>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

class PythonMT {
public:
    explicit PythonMT(uint32_t seed) { init_by_array(seed); }

    uint32_t word() {
        if (index_ >= 624) twist();
        uint32_t value = state_[index_++];
        value ^= value >> 11;
        value ^= (value << 7) & 0x9d2c5680U;
        value ^= (value << 15) & 0xefc60000U;
        value ^= value >> 18;
        return value;
    }

    uint32_t randbelow_900() {
        while (true) {
            const uint32_t value = word() >> 22;  // getrandbits(10)
            if (value < 900U) return value;
        }
    }

private:
    std::array<uint32_t, 624> state_{};
    int index_ = 624;

    void init_genrand(uint32_t seed) {
        state_[0] = seed;
        for (uint32_t index = 1; index < 624; ++index) {
            state_[index] = 1812433253U * (state_[index - 1] ^ (state_[index - 1] >> 30)) + index;
        }
        index_ = 624;
    }

    void init_by_array(uint32_t key) {
        init_genrand(19650218U);
        uint32_t i = 1;
        uint32_t j = 0;
        uint32_t count = 624;
        for (; count; --count) {
            state_[i] = (state_[i] ^ ((state_[i - 1] ^ (state_[i - 1] >> 30)) * 1664525U)) + key + j;
            ++i;
            if (++j >= 1) j = 0;
            if (i >= 624) {
                state_[0] = state_[623];
                i = 1;
            }
        }
        for (count = 623; count; --count) {
            state_[i] = (state_[i] ^ ((state_[i - 1] ^ (state_[i - 1] >> 30)) * 1566083941U)) - i;
            ++i;
            if (i >= 624) {
                state_[0] = state_[623];
                i = 1;
            }
        }
        state_[0] = 0x80000000U;
        index_ = 624;
    }

    void twist() {
        constexpr uint32_t matrix = 0x9908b0dfU;
        constexpr uint32_t upper = 0x80000000U;
        constexpr uint32_t lower = 0x7fffffffU;
        for (int i = 0; i < 227; ++i) {
            const uint32_t mixed = (state_[i] & upper) | (state_[i + 1] & lower);
            state_[i] = state_[i + 397] ^ (mixed >> 1) ^ ((mixed & 1U) ? matrix : 0U);
        }
        for (int i = 227; i < 623; ++i) {
            const uint32_t mixed = (state_[i] & upper) | (state_[i + 1] & lower);
            state_[i] = state_[i - 227] ^ (mixed >> 1) ^ ((mixed & 1U) ? matrix : 0U);
        }
        const uint32_t mixed = (state_[623] & upper) | (state_[0] & lower);
        state_[623] = state_[396] ^ (mixed >> 1) ^ ((mixed & 1U) ? matrix : 0U);
        index_ = 0;
    }
};

static std::array<uint32_t, 3> triplet(PythonMT& engine) {
    std::array<uint32_t, 3> output{};
    std::size_t count = 0;
    while (count < output.size()) {
        const uint32_t value = 100U + engine.randbelow_900();
        bool duplicate = false;
        for (std::size_t index = 0; index < count; ++index) duplicate |= output[index] == value;
        if (!duplicate) output[count++] = value;
    }
    return output;
}

static void emit_triplet(const std::array<uint32_t, 3>& values) {
    std::cout << '[' << values[0] << ',' << values[1] << ',' << values[2] << ']';
}

int main(int argc, char** argv) {
    if (argc == 3 && std::string(argv[1]) == "--emit") {
        PythonMT engine(static_cast<uint32_t>(std::stoull(argv[2])));
        emit_triplet(triplet(engine));
        std::cout << ' ';
        emit_triplet(triplet(engine));
        std::cout << '\n';
        return 0;
    }
    if (argc < 3 || argc > 4) {
        std::cerr << "usage: preseed_python_seed32_bruteforce START STOP_EXCLUSIVE [THREADS]\n";
        return 2;
    }
    const uint64_t start = std::stoull(argv[1]);
    const uint64_t stop = std::stoull(argv[2]);
    if (start > stop || stop > (uint64_t{1} << 32)) return 2;
    const int threads = argc == 4 ? std::max(1, std::stoi(argv[3])) : 1;
#ifdef _OPENMP
    omp_set_num_threads(threads);
#endif
    constexpr std::array<uint32_t, 3> prelude{654, 347, 964};
    constexpr std::array<uint32_t, 3> first_discovery{491, 210, 379};
    std::atomic<uint64_t> prelude_hits{0};
    std::mutex mutex;
    std::vector<uint32_t> exact_hits;
#pragma omp parallel
    {
        std::vector<uint32_t> local;
#pragma omp for schedule(static)
        for (uint64_t raw_seed = start; raw_seed < stop; ++raw_seed) {
            PythonMT engine(static_cast<uint32_t>(raw_seed));
            if (triplet(engine) != prelude) continue;
            prelude_hits.fetch_add(1, std::memory_order_relaxed);
            if (triplet(engine) == first_discovery) local.push_back(static_cast<uint32_t>(raw_seed));
        }
        if (!local.empty()) {
            std::lock_guard<std::mutex> lock(mutex);
            exact_hits.insert(exact_hits.end(), local.begin(), local.end());
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

// AVX2 exhaustive uint32 initializer search for NumPy RandomState/MT19937.
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <fstream>
#include <immintrin.h>
#include <iostream>
#include <mutex>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

constexpr int LANES = 8;
constexpr int PREFIX_WORDS = 24;

static void raw_prefix(uint32_t base, uint32_t output[PREFIX_WORDS][LANES]) {
    std::array<__m256i, 624> state{};
    state[0] = _mm256_setr_epi32(
        base + 0U, base + 1U, base + 2U, base + 3U,
        base + 4U, base + 5U, base + 6U, base + 7U
    );
    const __m256i multiplier = _mm256_set1_epi32(static_cast<int>(1812433253U));
    for (int index = 1; index < 624; ++index) {
        const __m256i prior = state[index - 1];
        const __m256i mixed = _mm256_xor_si256(prior, _mm256_srli_epi32(prior, 30));
        state[index] = _mm256_add_epi32(
            _mm256_mullo_epi32(multiplier, mixed), _mm256_set1_epi32(index)
        );
    }
    const __m256i upper = _mm256_set1_epi32(static_cast<int>(0x80000000U));
    const __m256i lower = _mm256_set1_epi32(0x7fffffffU);
    const __m256i matrix = _mm256_set1_epi32(static_cast<int>(0x9908b0dfU));
    auto twist_word = [&](int index, int source, int following) {
        const __m256i mixed = _mm256_or_si256(
            _mm256_and_si256(state[index], upper),
            _mm256_and_si256(state[following], lower)
        );
        const __m256i conditional = _mm256_mullo_epi32(
            _mm256_and_si256(mixed, _mm256_set1_epi32(1)), matrix
        );
        state[index] = _mm256_xor_si256(
            state[source], _mm256_xor_si256(_mm256_srli_epi32(mixed, 1), conditional)
        );
    };
    for (int index = 0; index < 227; ++index) twist_word(index, index + 397, index + 1);
    for (int index = 227; index < 623; ++index) twist_word(index, index - 227, index + 1);
    twist_word(623, 396, 0);
    alignas(32) uint32_t lanes[LANES];
    for (int index = 0; index < PREFIX_WORDS; ++index) {
        __m256i value = state[index];
        value = _mm256_xor_si256(value, _mm256_srli_epi32(value, 11));
        value = _mm256_xor_si256(
            value,
            _mm256_and_si256(_mm256_slli_epi32(value, 7), _mm256_set1_epi32(static_cast<int>(0x9d2c5680U)))
        );
        value = _mm256_xor_si256(
            value,
            _mm256_and_si256(_mm256_slli_epi32(value, 15), _mm256_set1_epi32(static_cast<int>(0xefc60000U)))
        );
        value = _mm256_xor_si256(value, _mm256_srli_epi32(value, 18));
        _mm256_store_si256(reinterpret_cast<__m256i*>(lanes), value);
        for (int lane = 0; lane < LANES; ++lane) output[index][lane] = lanes[lane];
    }
}

static uint32_t interval(std::mt19937& engine, uint32_t maximum) {
    uint32_t mask = maximum;
    mask |= mask >> 1; mask |= mask >> 2; mask |= mask >> 4;
    mask |= mask >> 8; mask |= mask >> 16;
    while (true) {
        const uint32_t value = engine() & mask;
        if (value <= maximum) return value;
    }
}

static std::array<uint32_t, 3> scalar_triplet(std::mt19937& engine) {
    std::array<uint32_t, 3> values{};
    std::size_t count = 0;
    while (count < values.size()) {
        const uint32_t value = 100U + interval(engine, 899U);
        if (std::find(values.begin(), values.begin() + count, value) == values.begin() + count) {
            values[count++] = value;
        }
    }
    return values;
}

static void shuffle_256(std::mt19937& engine) {
    for (uint32_t maximum = 255; maximum > 0; --maximum) (void)interval(engine, maximum);
}

static std::array<uint32_t, 3> scalar_choice_triplet(std::mt19937& engine) {
    std::array<uint32_t, 900> values{};
    for (uint32_t index = 0; index < values.size(); ++index) values[index] = 100U + index;
    for (uint32_t maximum = 899; maximum > 0; --maximum) {
        const uint32_t choice = interval(engine, maximum);
        std::swap(values[maximum], values[choice]);
    }
    return {values[0], values[1], values[2]};
}

// 1=match, 0=definite mismatch, -1=prefix exhausted and scalar fallback needed.
static int prefix_status(const uint32_t raw[PREFIX_WORDS][LANES], int lane) {
    constexpr std::array<uint32_t, 3> expected{654, 347, 964};
    std::array<uint32_t, 3> values{};
    int cursor = 0;
    std::size_t count = 0;
    while (count < values.size() && cursor < PREFIX_WORDS) {
        const uint32_t candidate = raw[cursor++][lane] & 1023U;
        if (candidate > 899U) continue;
        const uint32_t value = 100U + candidate;
        if (std::find(values.begin(), values.begin() + count, value) == values.begin() + count) {
            values[count++] = value;
        }
    }
    if (count != values.size()) return -1;
    return values == expected ? 1 : 0;
}

static int shuffle_prefix_status(
    const uint32_t raw[PREFIX_WORDS][LANES],
    int lane,
    const std::array<bool, 65536>& allowed_pairs,
    const std::vector<uint32_t>& allowed_keys
) {
    uint32_t key = 0;
    int cursor = 0;
    for (uint32_t offset = 0; offset < 4; ++offset) {
        const uint32_t maximum = 255U - offset;
        int accepted = -1;
        while (cursor < PREFIX_WORDS) {
            const uint32_t value = raw[cursor++][lane] & 255U;
            if (value <= maximum) {
                accepted = static_cast<int>(value);
                break;
            }
        }
        if (accepted < 0) return -1;
        key |= static_cast<uint32_t>(accepted) << (offset * 8U);
        if (offset == 1 && !allowed_pairs[key & 0xffffU]) return 0;
    }
    return std::binary_search(allowed_keys.begin(), allowed_keys.end(), key) ? 1 : 0;
}

static uint32_t scalar_shuffle_prefix(std::mt19937& engine) {
    uint32_t key = 0;
    for (uint32_t offset = 0; offset < 4; ++offset) {
        key |= interval(engine, 255U - offset) << (offset * 8U);
    }
    return key;
}

static std::vector<uint32_t> load_keys(const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("could not open tuple-key file");
    std::vector<uint32_t> values;
    uint64_t value = 0;
    while (input >> value) {
        if (value > 0xffffffffULL) throw std::runtime_error("tuple key exceeds uint32");
        values.push_back(static_cast<uint32_t>(value));
    }
    std::sort(values.begin(), values.end());
    values.erase(std::unique(values.begin(), values.end()), values.end());
    if (values.empty()) throw std::runtime_error("tuple-key file is empty");
    return values;
}

static bool self_test() {
    constexpr std::array<uint32_t, 5> choice_seeds{0U, 1U, 42U, 12345U, 0xffffffffU};
    constexpr std::array<std::array<uint32_t, 3>, 5> integer_expected{{
        {784U, 659U, 729U}, {137U, 335U, 172U}, {202U, 535U, 960U},
        {582U, 585U, 385U}, {519U, 390U, 112U}
    }};
    constexpr std::array<std::array<uint32_t, 3>, 5> choice_expected{{
        {592U, 241U, 509U}, {960U, 980U, 974U}, {170U, 927U, 331U},
        {358U, 244U, 217U}, {827U, 166U, 967U}
    }};
    for (std::size_t index = 0; index < choice_seeds.size(); ++index) {
        std::mt19937 integer_engine(choice_seeds[index]);
        if (scalar_triplet(integer_engine) != integer_expected[index]) return false;
        std::mt19937 engine(choice_seeds[index]);
        if (scalar_choice_triplet(engine) != choice_expected[index]) return false;
    }
    for (uint32_t base : {0U, 8U, 40U, 12344U, 0xfffffff8U}) {
        alignas(32) uint32_t words[PREFIX_WORDS][LANES];
        raw_prefix(base, words);
        for (int lane = 0; lane < LANES; ++lane) {
            std::mt19937 engine(base + static_cast<uint32_t>(lane));
            for (int index = 0; index < PREFIX_WORDS; ++index) {
                if (words[index][lane] != engine()) return false;
            }
            std::mt19937 shuffle_engine(base + static_cast<uint32_t>(lane));
            const uint32_t key = scalar_shuffle_prefix(shuffle_engine);
            const std::vector<uint32_t> keys{key};
            std::array<bool, 65536> pairs{};
            pairs[key & 0xffffU] = true;
            if (shuffle_prefix_status(words, lane, pairs, keys) != 1) return false;
        }
    }
    return true;
}

static uint32_t triplet_key(const std::array<uint32_t, 3>& values) {
    return values[0] | (values[1] << 10U) | (values[2] << 20U);
}

static std::vector<std::array<uint32_t, 3>> load_targets(const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("could not open target file");
    std::vector<std::array<uint32_t, 3>> targets;
    std::string line;
    while (std::getline(input, line)) {
        if (line.empty()) continue;
        std::replace(line.begin(), line.end(), ',', ' ');
        std::istringstream values(line);
        std::array<uint32_t, 3> target{};
        if (!(values >> target[0] >> target[1] >> target[2])) {
            throw std::runtime_error("target lines require three integers");
        }
        std::string trailing;
        if (values >> trailing) throw std::runtime_error("target line has trailing data");
        for (uint32_t value : target) {
            if (value < 100U || value > 999U) throw std::runtime_error("target outside 100..999");
        }
        targets.push_back(target);
    }
    if (targets.empty()) throw std::runtime_error("target file is empty");
    return targets;
}

static std::array<uint32_t, 3> prefix_triplet(
    const uint32_t raw[PREFIX_WORDS][LANES], int lane
) {
    std::array<uint32_t, 3> values{};
    int cursor = 0;
    std::size_t count = 0;
    while (count < values.size() && cursor < PREFIX_WORDS) {
        uint32_t value = raw[cursor++][lane] & 1023U;
        if (value > 899U) continue;
        value += 100U;
        if (std::find(values.begin(), values.begin() + count, value) == values.begin() + count) {
            values[count++] = value;
        }
    }
    if (count != values.size()) return {0U, 0U, 0U};
    return values;
}

static int run_multi_target(
    const std::string& path, uint64_t start, uint64_t stop, int threads
) {
    const auto targets = load_targets(path);
    std::unordered_map<uint32_t, std::vector<std::size_t>> target_indices;
    for (std::size_t index = 0; index < targets.size(); ++index) {
        target_indices[triplet_key(targets[index])].push_back(index);
    }
    std::vector<std::vector<uint32_t>> hits(targets.size());
    const uint64_t batches = (stop - start + LANES - 1) / LANES;
    std::mutex mutex;
#ifdef _OPENMP
    omp_set_num_threads(threads);
#endif
#pragma omp parallel
    {
        std::vector<std::pair<std::size_t, uint32_t>> local;
#pragma omp for schedule(static)
        for (uint64_t batch = 0; batch < batches; ++batch) {
            const uint64_t base64 = start + batch * LANES;
            alignas(32) uint32_t words[PREFIX_WORDS][LANES];
            raw_prefix(static_cast<uint32_t>(base64), words);
            for (int lane = 0; lane < LANES; ++lane) {
                const uint64_t seed64 = base64 + static_cast<uint64_t>(lane);
                if (seed64 >= stop) continue;
                auto values = prefix_triplet(words, lane);
                if (values[0] == 0U) {
                    std::mt19937 engine(static_cast<uint32_t>(seed64));
                    values = scalar_triplet(engine);
                }
                const auto found = target_indices.find(triplet_key(values));
                if (found == target_indices.end()) continue;
                for (std::size_t target : found->second) {
                    local.emplace_back(target, static_cast<uint32_t>(seed64));
                }
            }
        }
        if (!local.empty()) {
            std::lock_guard<std::mutex> lock(mutex);
            for (const auto& [target, seed] : local) hits[target].push_back(seed);
        }
    }
    for (auto& values : hits) std::sort(values.begin(), values.end());
    std::cout << "{\"mode\":\"multi-target\",\"start\":" << start
              << ",\"stop_exclusive\":" << stop << ",\"tested\":" << (stop - start)
              << ",\"targets\":[";
    for (std::size_t index = 0; index < targets.size(); ++index) {
        if (index) std::cout << ',';
        std::cout << "{\"target\":[" << targets[index][0] << ',' << targets[index][1] << ','
                  << targets[index][2] << "],\"integer_candidates\":[";
        for (std::size_t hit = 0; hit < hits[index].size(); ++hit) {
            if (hit) std::cout << ',';
            std::cout << hits[index][hit];
        }
        std::cout << "]}";
    }
    std::cout << "]}\n";
    return 0;
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--self-test") {
        const bool okay = self_test();
        std::cout << (okay ? "ok" : "failed") << '\n';
        return okay ? 0 : 1;
    }
    if (argc == 6 && std::string(argv[1]) == "--multi-target") {
        const uint64_t start = std::stoull(argv[3]);
        const uint64_t stop = std::stoull(argv[4]);
        if (start > stop || stop > (uint64_t{1} << 32) || (start % LANES) != 0) return 2;
        return run_multi_target(argv[2], start, stop, std::max(1, std::stoi(argv[5])));
    }
    const bool shuffle_target = argc == 6 && std::string(argv[1]) == "--shuffle-target";
    if (!shuffle_target && (argc < 3 || argc > 4)) return 2;
    const int argument_offset = shuffle_target ? 2 : 0;
    const std::vector<uint32_t> allowed_keys = shuffle_target
        ? load_keys(argv[argument_offset])
        : std::vector<uint32_t>{};
    std::array<bool, 65536> allowed_pairs{};
    for (const uint32_t key : allowed_keys) allowed_pairs[key & 0xffffU] = true;
    const uint64_t start = std::stoull(argv[argument_offset + 1]);
    const uint64_t stop = std::stoull(argv[argument_offset + 2]);
    if (start > stop || stop > (uint64_t{1} << 32) || (start % LANES) != 0) return 2;
    const int threads = (shuffle_target || argc == 4)
        ? std::max(1, std::stoi(argv[argument_offset + 3]))
        : 1;
#ifdef _OPENMP
    omp_set_num_threads(threads);
#endif
    constexpr std::array<uint32_t, 3> prelude{654, 347, 964};
    constexpr std::array<uint32_t, 3> first_discovery{491, 210, 379};
    const uint64_t batches = (stop - start + LANES - 1) / LANES;
    std::atomic<uint64_t> prelude_hits{0};
    std::mutex mutex;
    std::vector<uint32_t> prelude_candidates;
    std::vector<uint32_t> exact_hits;
    std::vector<uint32_t> choice_hits;
#pragma omp parallel
    {
        std::vector<uint32_t> local_prelude;
        std::vector<uint32_t> local;
        std::vector<uint32_t> local_choice;
#pragma omp for schedule(static)
        for (uint64_t batch = 0; batch < batches; ++batch) {
            const uint64_t base64 = start + batch * LANES;
            alignas(32) uint32_t words[PREFIX_WORDS][LANES];
            raw_prefix(static_cast<uint32_t>(base64), words);
            for (int lane = 0; lane < LANES; ++lane) {
                const uint64_t seed64 = base64 + static_cast<uint64_t>(lane);
                if (seed64 >= stop) continue;
                const int status = shuffle_target
                    ? shuffle_prefix_status(words, lane, allowed_pairs, allowed_keys)
                    : prefix_status(words, lane);
                if (status == 0) continue;
                std::mt19937 engine(static_cast<uint32_t>(seed64));
                if (shuffle_target) {
                    const uint32_t key = scalar_shuffle_prefix(engine);
                    if (!std::binary_search(allowed_keys.begin(), allowed_keys.end(), key)) continue;
                } else if (scalar_triplet(engine) != prelude) {
                    continue;  // exact fallback verification
                }
                prelude_hits.fetch_add(1, std::memory_order_relaxed);
                local_prelude.push_back(static_cast<uint32_t>(seed64));
                if (shuffle_target) {
                    for (uint32_t maximum = 251; maximum > 0; --maximum) {
                        (void)interval(engine, maximum);
                    }
                } else {
                    shuffle_256(engine);
                }
                std::mt19937 choice_engine = engine;
                if (scalar_triplet(engine) == first_discovery) local.push_back(static_cast<uint32_t>(seed64));
                if (shuffle_target && scalar_choice_triplet(choice_engine) == first_discovery) {
                    local_choice.push_back(static_cast<uint32_t>(seed64));
                }
            }
        }
        if (!local.empty() || !local_prelude.empty() || !local_choice.empty()) {
            std::lock_guard<std::mutex> lock(mutex);
            prelude_candidates.insert(prelude_candidates.end(), local_prelude.begin(), local_prelude.end());
            exact_hits.insert(exact_hits.end(), local.begin(), local.end());
            choice_hits.insert(choice_hits.end(), local_choice.begin(), local_choice.end());
        }
    }
    std::sort(prelude_candidates.begin(), prelude_candidates.end());
    std::sort(exact_hits.begin(), exact_hits.end());
    std::sort(choice_hits.begin(), choice_hits.end());
    std::cout << "{\"mode\":\"" << (shuffle_target ? "shuffle-target" : "prelude")
              << "\",\"start\":" << start << ",\"stop_exclusive\":" << stop
              << ",\"tested\":" << (stop - start)
              << ",\"prelude_hits\":" << prelude_hits.load()
              << ",\"prelude_candidates\":[";
    for (std::size_t index = 0; index < prelude_candidates.size(); ++index) {
        if (index) std::cout << ',';
        std::cout << prelude_candidates[index];
    }
    std::cout << "]"
              << ",\"first_discovery_hits\":[";
    for (std::size_t index = 0; index < exact_hits.size(); ++index) {
        if (index) std::cout << ',';
        std::cout << exact_hits[index];
    }
    std::cout << "]"
              << ",\"choice_first_discovery_hits\":[";
    for (std::size_t index = 0; index < choice_hits.size(); ++index) {
        if (index) std::cout << ',';
        std::cout << choice_hits[index];
    }
    std::cout << "]}\n";
    return 0;
}

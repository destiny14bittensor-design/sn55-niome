// Exhaustive uint32 initializer scan for NumPy default_rng/PCG64.
//
// This is a clean-room transcription of NumPy 2.5's public SeedSequence,
// PCG64 initialization, next_uint32 buffering, and Lemire bounded uint32
// mapping.  It tests a persistent ``default_rng(seed).integers(100, 1000)``
// stream against an ordered list of triplets.  Only candidates matching the
// first triplet are replayed against the remaining Discovery labels.

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr uint32_t INIT_A = 0x43b0d7e5U;
constexpr uint32_t MULT_A = 0x931e8875U;
constexpr uint32_t INIT_B = 0x8b51f9ddU;
constexpr uint32_t MULT_B = 0x58f38dedU;
constexpr uint32_t MIX_MULT_L = 0xca01f9ddU;
constexpr uint32_t MIX_MULT_R = 0x4973f715U;
constexpr unsigned __int128 PCG_MULT =
    (static_cast<unsigned __int128>(2549297995355413924ULL) << 64U) |
    static_cast<unsigned __int128>(4865540595714422341ULL);
constexpr uint64_t PCG_DXSM_MULT = 0xda942042e4dd58b5ULL;

struct Triplet {
  uint32_t value[3]{};
};

uint32_t hashmix(uint32_t value, uint32_t &hash_const) {
  value ^= hash_const;
  hash_const *= MULT_A;
  value *= hash_const;
  value ^= value >> 16U;
  return value;
}

uint32_t mix(uint32_t x, uint32_t y) {
  uint32_t result = MIX_MULT_L * x - MIX_MULT_R * y;
  result ^= result >> 16U;
  return result;
}

void seed_sequence_words(uint32_t entropy, uint64_t out[4]) {
  uint32_t pool[4]{};
  uint32_t hash_const = INIT_A;
  for (int i = 0; i < 4; ++i) {
    pool[i] = hashmix(i == 0 ? entropy : 0U, hash_const);
  }
  for (int source = 0; source < 4; ++source) {
    for (int destination = 0; destination < 4; ++destination) {
      if (source != destination) {
        pool[destination] =
            mix(pool[destination], hashmix(pool[source], hash_const));
      }
    }
  }
  uint32_t generated[8]{};
  hash_const = INIT_B;
  for (int i = 0; i < 8; ++i) {
    uint32_t value = pool[i % 4];
    value ^= hash_const;
    hash_const *= MULT_B;
    value *= hash_const;
    value ^= value >> 16U;
    generated[i] = value;
  }
  for (int i = 0; i < 4; ++i) {
    out[i] = static_cast<uint64_t>(generated[2 * i]) |
             (static_cast<uint64_t>(generated[2 * i + 1]) << 32U);
  }
}

uint64_t rotate_right(uint64_t value, uint32_t rotation) {
  rotation &= 63U;
  return (value >> rotation) | (value << ((-rotation) & 63U));
}

struct Pcg64 {
  unsigned __int128 state{};
  unsigned __int128 increment{};
  bool has_uint32{false};
  uint32_t cached_uint32{};
  bool dxsm{false};

  explicit Pcg64(uint32_t seed, bool use_dxsm = false) : dxsm(use_dxsm) {
    uint64_t words[4]{};
    seed_sequence_words(seed, words);
    const unsigned __int128 initial_state =
        (static_cast<unsigned __int128>(words[0]) << 64U) | words[1];
    const unsigned __int128 initial_sequence =
        (static_cast<unsigned __int128>(words[2]) << 64U) | words[3];
    increment = (initial_sequence << 1U) | 1U;
    step();
    state += initial_state;
    step();
  }

  void step() { state = state * PCG_MULT + increment; }

  uint64_t next64() {
    if (dxsm) {
      const uint64_t high_initial = static_cast<uint64_t>(state >> 64U);
      const uint64_t low = static_cast<uint64_t>(state) | 1ULL;
      uint64_t high = high_initial ^ (high_initial >> 32U);
      high *= PCG_DXSM_MULT;
      high ^= high >> 48U;
      high *= low;
      state = state * static_cast<unsigned __int128>(PCG_DXSM_MULT) + increment;
      return high;
    }
    step();
    const uint64_t high = static_cast<uint64_t>(state >> 64U);
    const uint64_t low = static_cast<uint64_t>(state);
    return rotate_right(high ^ low, static_cast<uint32_t>(high >> 58U));
  }

  uint32_t next32() {
    if (has_uint32) {
      has_uint32 = false;
      return cached_uint32;
    }
    const uint64_t value = next64();
    has_uint32 = true;
    cached_uint32 = static_cast<uint32_t>(value >> 32U);
    return static_cast<uint32_t>(value);
  }

  uint32_t bounded900() {
    constexpr uint32_t range = 900U;
    constexpr uint32_t threshold =
        static_cast<uint32_t>((UINT32_MAX - (range - 1U)) % range);
    while (true) {
      const uint64_t product = static_cast<uint64_t>(next32()) * range;
      if (static_cast<uint32_t>(product) >= threshold) {
        return static_cast<uint32_t>(product >> 32U) + 100U;
      }
    }
  }

  uint32_t bounded(uint32_t range) {
    const uint32_t maximum = range - 1U;
    const uint32_t threshold = static_cast<uint32_t>((UINT32_MAX - maximum) % range);
    while (true) {
      const uint64_t product = static_cast<uint64_t>(next32()) * range;
      if (static_cast<uint32_t>(product) >= threshold) {
        return static_cast<uint32_t>(product >> 32U);
      }
    }
  }

  Triplet unique_triplet() {
    Triplet result{};
    int count = 0;
    while (count < 3) {
      const uint32_t value = bounded900();
      bool duplicate = false;
      for (int i = 0; i < count; ++i) {
        duplicate |= result.value[i] == value;
      }
      if (!duplicate) result.value[count++] = value;
    }
    return result;
  }

  Triplet float_triplet() {
    Triplet result{};
    int count = 0;
    while (count < 3) {
      const double uniform = static_cast<double>(next64() >> 11U) * 0x1.0p-53;
      const uint32_t value = 100U + static_cast<uint32_t>(uniform * 900.0);
      bool duplicate = false;
      for (int i = 0; i < count; ++i) duplicate |= result.value[i] == value;
      if (!duplicate) result.value[count++] = value;
    }
    return result;
  }

  Triplet choice_triplet() {
    // NumPy Generator.choice(900, 3, replace=False) uses Floyd's algorithm
    // followed by a two-step shuffle for this population/sample ratio.
    Triplet result{};
    uint32_t selected[3]{};
    int count = 0;
    for (uint32_t j = 897U; j < 900U; ++j) {
      const uint32_t value = bounded(j + 1U);
      bool duplicate = false;
      for (int i = 0; i < count; ++i) duplicate |= selected[i] == value;
      const uint32_t picked = duplicate ? j : value;
      selected[count] = picked;
      result.value[count++] = picked;
    }
    for (int i = 2; i >= 1; --i) {
      const uint32_t j = bounded(static_cast<uint32_t>(i + 1));
      std::swap(result.value[i], result.value[j]);
    }
    for (uint32_t &value : result.value) value += 100U;
    return result;
  }
};

struct Sfc64 {
  uint64_t state[4]{};
  bool has_uint32{false};
  uint32_t cached_uint32{};

  explicit Sfc64(uint32_t seed) {
    uint64_t words[4]{};
    seed_sequence_words(seed, words);
    state[0] = words[0];
    state[1] = words[1];
    state[2] = words[2];
    state[3] = 1;
    for (int i = 0; i < 12; ++i) (void)next64();
  }

  uint64_t next64() {
    const uint64_t result = state[0] + state[1] + state[3]++;
    state[0] = state[1] ^ (state[1] >> 11U);
    state[1] = state[2] + (state[2] << 3U);
    state[2] = ((state[2] << 24U) | (state[2] >> 40U)) + result;
    return result;
  }

  uint32_t next32() {
    if (has_uint32) {
      has_uint32 = false;
      return cached_uint32;
    }
    const uint64_t value = next64();
    has_uint32 = true;
    cached_uint32 = static_cast<uint32_t>(value >> 32U);
    return static_cast<uint32_t>(value);
  }

  uint32_t bounded(uint32_t range) {
    const uint32_t maximum = range - 1U;
    const uint32_t threshold = static_cast<uint32_t>((UINT32_MAX - maximum) % range);
    while (true) {
      const uint64_t product = static_cast<uint64_t>(next32()) * range;
      if (static_cast<uint32_t>(product) >= threshold) {
        return static_cast<uint32_t>(product >> 32U);
      }
    }
  }

  Triplet unique_triplet() {
    Triplet result{};
    int count = 0;
    while (count < 3) {
      const uint32_t value = bounded(900U) + 100U;
      bool duplicate = false;
      for (int i = 0; i < count; ++i) duplicate |= result.value[i] == value;
      if (!duplicate) result.value[count++] = value;
    }
    return result;
  }

  Triplet float_triplet() {
    Triplet result{};
    int count = 0;
    while (count < 3) {
      const double uniform = static_cast<double>(next64() >> 11U) * 0x1.0p-53;
      const uint32_t value = 100U + static_cast<uint32_t>(uniform * 900.0);
      bool duplicate = false;
      for (int i = 0; i < count; ++i) duplicate |= result.value[i] == value;
      if (!duplicate) result.value[count++] = value;
    }
    return result;
  }

  Triplet choice_triplet() {
    Triplet result{};
    uint32_t selected[3]{};
    int count = 0;
    for (uint32_t j = 897U; j < 900U; ++j) {
      const uint32_t value = bounded(j + 1U);
      bool duplicate = false;
      for (int i = 0; i < count; ++i) duplicate |= selected[i] == value;
      const uint32_t picked = duplicate ? j : value;
      selected[count] = picked;
      result.value[count++] = picked;
    }
    for (int i = 2; i >= 1; --i) {
      const uint32_t j = bounded(static_cast<uint32_t>(i + 1));
      std::swap(result.value[i], result.value[j]);
    }
    for (uint32_t &value : result.value) value += 100U;
    return result;
  }
};

struct Philox {
  uint64_t counter[4]{};
  uint64_t key[2]{};
  uint64_t buffer[4]{};
  int buffer_position{4};
  bool has_uint32{false};
  uint32_t cached_uint32{};

  explicit Philox(uint32_t seed) {
    uint64_t words[4]{};
    seed_sequence_words(seed, words);
    key[0] = words[0];
    key[1] = words[1];
  }

  void generate_block() {
    if (++counter[0] == 0 && ++counter[1] == 0 && ++counter[2] == 0) {
      ++counter[3];
    }
    uint64_t values[4] = {counter[0], counter[1], counter[2], counter[3]};
    uint64_t round_key[2] = {key[0], key[1]};
    for (int round = 0; round < 10; ++round) {
      const unsigned __int128 product0 =
          static_cast<unsigned __int128>(0xD2E7470EE14C6C93ULL) * values[0];
      const unsigned __int128 product1 =
          static_cast<unsigned __int128>(0xCA5A826395121157ULL) * values[2];
      const uint64_t low0 = static_cast<uint64_t>(product0);
      const uint64_t high0 = static_cast<uint64_t>(product0 >> 64U);
      const uint64_t low1 = static_cast<uint64_t>(product1);
      const uint64_t high1 = static_cast<uint64_t>(product1 >> 64U);
      const uint64_t next[4] = {
          high1 ^ values[1] ^ round_key[0], low1,
          high0 ^ values[3] ^ round_key[1], low0};
      std::copy(std::begin(next), std::end(next), values);
      if (round != 9) {
        round_key[0] += 0x9E3779B97F4A7C15ULL;
        round_key[1] += 0xBB67AE8584CAA73BULL;
      }
    }
    std::copy(std::begin(values), std::end(values), buffer);
    buffer_position = 0;
  }

  uint64_t next64() {
    if (buffer_position >= 4) generate_block();
    return buffer[buffer_position++];
  }

  uint32_t next32() {
    if (has_uint32) {
      has_uint32 = false;
      return cached_uint32;
    }
    const uint64_t value = next64();
    has_uint32 = true;
    cached_uint32 = static_cast<uint32_t>(value >> 32U);
    return static_cast<uint32_t>(value);
  }

  uint32_t bounded(uint32_t range) {
    const uint32_t maximum = range - 1U;
    const uint32_t threshold = static_cast<uint32_t>((UINT32_MAX - maximum) % range);
    while (true) {
      const uint64_t product = static_cast<uint64_t>(next32()) * range;
      if (static_cast<uint32_t>(product) >= threshold) {
        return static_cast<uint32_t>(product >> 32U);
      }
    }
  }

  Triplet unique_triplet() {
    Triplet result{};
    int count = 0;
    while (count < 3) {
      const uint32_t value = bounded(900U) + 100U;
      bool duplicate = false;
      for (int i = 0; i < count; ++i) duplicate |= result.value[i] == value;
      if (!duplicate) result.value[count++] = value;
    }
    return result;
  }

  Triplet float_triplet() {
    Triplet result{};
    int count = 0;
    while (count < 3) {
      const double uniform = static_cast<double>(next64() >> 11U) * 0x1.0p-53;
      const uint32_t value = 100U + static_cast<uint32_t>(uniform * 900.0);
      bool duplicate = false;
      for (int i = 0; i < count; ++i) duplicate |= result.value[i] == value;
      if (!duplicate) result.value[count++] = value;
    }
    return result;
  }

  Triplet choice_triplet() {
    Triplet result{};
    uint32_t selected[3]{};
    int count = 0;
    for (uint32_t j = 897U; j < 900U; ++j) {
      const uint32_t value = bounded(j + 1U);
      bool duplicate = false;
      for (int i = 0; i < count; ++i) duplicate |= selected[i] == value;
      const uint32_t picked = duplicate ? j : value;
      selected[count] = picked;
      result.value[count++] = picked;
    }
    for (int i = 2; i >= 1; --i) {
      const uint32_t j = bounded(static_cast<uint32_t>(i + 1));
      std::swap(result.value[i], result.value[j]);
    }
    for (uint32_t &value : result.value) value += 100U;
    return result;
  }
};

bool equal(const Triplet &left, const Triplet &right) {
  return left.value[0] == right.value[0] &&
         left.value[1] == right.value[1] &&
         left.value[2] == right.value[2];
}

std::vector<Triplet> load_targets(const std::string &path) {
  std::ifstream input(path);
  if (!input) throw std::runtime_error("cannot open target file");
  std::vector<Triplet> targets;
  std::string line;
  while (std::getline(input, line)) {
    if (line.empty()) continue;
    std::replace(line.begin(), line.end(), ',', ' ');
    std::istringstream parser(line);
    Triplet target{};
    if (!(parser >> target.value[0] >> target.value[1] >> target.value[2])) {
      throw std::runtime_error("invalid target line");
    }
    targets.push_back(target);
  }
  if (targets.empty()) throw std::runtime_error("target file is empty");
  return targets;
}

}  // namespace

int main(int argc, char **argv) {
  if (argc < 6) {
    std::cerr << "usage: scanner TARGETS START STOP_EXCLUSIVE THREADS MODE [ENGINE]\n";
    return 2;
  }
  const auto targets = load_targets(argv[1]);
  const uint64_t start = std::stoull(argv[2]);
  const uint64_t stop = std::stoull(argv[3]);
  const unsigned threads = std::max(1U, static_cast<unsigned>(std::stoul(argv[4])));
  const std::string mode = argv[5];
  const std::string engine = argc >= 7 ? argv[6] : "pcg64";
  if (stop > (uint64_t{1} << 32U) || stop < start ||
      (mode != "integers-unique" && mode != "choice" && mode != "float-unique") ||
      (engine != "pcg64" && engine != "pcg64dxsm" && engine != "sfc64" &&
       engine != "philox")) {
    std::cerr << "invalid range or mode\n";
    return 2;
  }

  std::mutex output_mutex;
  std::vector<uint32_t> first_hits;
  std::vector<uint32_t> full_hits;
  std::atomic<uint64_t> tested{0};
  auto worker = [&](unsigned rank) {
    std::vector<uint32_t> local_first;
    std::vector<uint32_t> local_full;
    uint64_t local_tested = 0;
    for (uint64_t candidate = start + rank; candidate < stop; candidate += threads) {
      bool first_match = false;
      bool full_match = false;
      const auto replay = [&](auto &generator) {
        const auto draw = [&]() {
          if (mode == "choice") return generator.choice_triplet();
          if (mode == "float-unique") return generator.float_triplet();
          return generator.unique_triplet();
        };
        first_match = equal(draw(), targets[0]);
        full_match = first_match;
        for (size_t index = 1; full_match && index < targets.size(); ++index) {
          full_match = equal(draw(), targets[index]);
        }
      };
      if (engine == "sfc64") {
        Sfc64 generator(static_cast<uint32_t>(candidate));
        replay(generator);
      } else if (engine == "philox") {
        Philox generator(static_cast<uint32_t>(candidate));
        replay(generator);
      } else {
        Pcg64 generator(static_cast<uint32_t>(candidate), engine == "pcg64dxsm");
        replay(generator);
      }
      if (first_match) {
        local_first.push_back(static_cast<uint32_t>(candidate));
        if (full_match) local_full.push_back(static_cast<uint32_t>(candidate));
      }
      ++local_tested;
    }
    tested.fetch_add(local_tested, std::memory_order_relaxed);
    std::lock_guard<std::mutex> guard(output_mutex);
    first_hits.insert(first_hits.end(), local_first.begin(), local_first.end());
    full_hits.insert(full_hits.end(), local_full.begin(), local_full.end());
  };
  std::vector<std::thread> workers;
  for (unsigned rank = 0; rank < threads; ++rank) workers.emplace_back(worker, rank);
  for (auto &thread : workers) thread.join();
  std::sort(first_hits.begin(), first_hits.end());
  std::sort(full_hits.begin(), full_hits.end());

  std::cout << "{\"engine\":\"numpy-" << engine << "\",\"method\":\"" << mode << "\""
            << ",\"start\":" << start << ",\"stop_exclusive\":" << stop
            << ",\"tested\":" << tested.load() << ",\"target_count\":"
            << targets.size() << ",\"first_triplet_candidates\":[";
  for (size_t i = 0; i < first_hits.size(); ++i) {
    if (i) std::cout << ',';
    std::cout << first_hits[i];
  }
  std::cout << "],\"full_stream_candidates\":[";
  for (size_t i = 0; i < full_hits.size(); ++i) {
    if (i) std::cout << ',';
    std::cout << full_hits[i];
  }
  std::cout << "]}\n";
  return 0;
}

// AVX2 exhaustive uint32 initializer search for CPython random.Random.
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

class PythonMT {
public:
    explicit PythonMT(uint32_t key) {
        state_[0] = 19650218U;
        for (uint32_t i = 1; i < 624; ++i) state_[i] = 1812433253U * (state_[i - 1] ^ (state_[i - 1] >> 30)) + i;
        uint32_t i = 1;
        for (uint32_t count = 624; count; --count) {
            state_[i] = (state_[i] ^ ((state_[i - 1] ^ (state_[i - 1] >> 30)) * 1664525U)) + key;
            if (++i >= 624) { state_[0] = state_[623]; i = 1; }
        }
        for (uint32_t count = 623; count; --count) {
            state_[i] = (state_[i] ^ ((state_[i - 1] ^ (state_[i - 1] >> 30)) * 1566083941U)) - i;
            if (++i >= 624) { state_[0] = state_[623]; i = 1; }
        }
        state_[0] = 0x80000000U;
    }
    uint32_t word() {
        if (index_ >= 624) twist();
        uint32_t value = state_[index_++];
        value ^= value >> 11; value ^= (value << 7) & 0x9d2c5680U;
        value ^= (value << 15) & 0xefc60000U; value ^= value >> 18;
        return value;
    }
    uint32_t below() { while (true) { uint32_t value = word() >> 22; if (value < 900U) return value; } }
    double random53() {
        const uint64_t high = word() >> 5;
        const uint64_t low = word() >> 6;
        return (high * 67108864.0 + low) / 9007199254740992.0;
    }
private:
    std::array<uint32_t, 624> state_{};
    int index_ = 624;
    void twist() {
        constexpr uint32_t matrix = 0x9908b0dfU, upper = 0x80000000U, lower = 0x7fffffffU;
        for (int i = 0; i < 227; ++i) { uint32_t y=(state_[i]&upper)|(state_[i+1]&lower); state_[i]=state_[i+397]^(y>>1)^((y&1)?matrix:0); }
        for (int i = 227; i < 623; ++i) { uint32_t y=(state_[i]&upper)|(state_[i+1]&lower); state_[i]=state_[i-227]^(y>>1)^((y&1)?matrix:0); }
        uint32_t y=(state_[623]&upper)|(state_[0]&lower); state_[623]=state_[396]^(y>>1)^((y&1)?matrix:0); index_=0;
    }
};

static void raw_prefix(uint32_t base, uint32_t output[PREFIX_WORDS][LANES]) {
    std::array<__m256i, 624> state{};
    state[0] = _mm256_set1_epi32(19650218);
    const __m256i init_multiplier = _mm256_set1_epi32(static_cast<int>(1812433253U));
    for (int i = 1; i < 624; ++i) {
        __m256i prior=state[i-1];
        state[i]=_mm256_add_epi32(_mm256_mullo_epi32(init_multiplier,_mm256_xor_si256(prior,_mm256_srli_epi32(prior,30))),_mm256_set1_epi32(i));
    }
    const __m256i keys = _mm256_setr_epi32(base+0U,base+1U,base+2U,base+3U,base+4U,base+5U,base+6U,base+7U);
    const __m256i multiplier1 = _mm256_set1_epi32(1664525);
    int i=1;
    for (int count=624; count; --count) {
        __m256i prior=state[i-1];
        __m256i mixed=_mm256_mullo_epi32(multiplier1,_mm256_xor_si256(prior,_mm256_srli_epi32(prior,30)));
        state[i]=_mm256_add_epi32(_mm256_xor_si256(state[i],mixed),keys);
        if (++i>=624) { state[0]=state[623]; i=1; }
    }
    const __m256i multiplier2 = _mm256_set1_epi32(static_cast<int>(1566083941U));
    for (int count=623; count; --count) {
        __m256i prior=state[i-1];
        __m256i mixed=_mm256_mullo_epi32(multiplier2,_mm256_xor_si256(prior,_mm256_srli_epi32(prior,30)));
        state[i]=_mm256_sub_epi32(_mm256_xor_si256(state[i],mixed),_mm256_set1_epi32(i));
        if (++i>=624) { state[0]=state[623]; i=1; }
    }
    state[0]=_mm256_set1_epi32(static_cast<int>(0x80000000U));
    const __m256i upper=_mm256_set1_epi32(static_cast<int>(0x80000000U)), lower=_mm256_set1_epi32(0x7fffffffU), matrix=_mm256_set1_epi32(static_cast<int>(0x9908b0dfU));
    auto twist_word=[&](int index,int source,int following){
        __m256i y=_mm256_or_si256(_mm256_and_si256(state[index],upper),_mm256_and_si256(state[following],lower));
        __m256i conditional=_mm256_mullo_epi32(_mm256_and_si256(y,_mm256_set1_epi32(1)),matrix);
        state[index]=_mm256_xor_si256(state[source],_mm256_xor_si256(_mm256_srli_epi32(y,1),conditional));
    };
    for (int index=0;index<227;++index) twist_word(index,index+397,index+1);
    for (int index=227;index<623;++index) twist_word(index,index-227,index+1);
    twist_word(623,396,0);
    alignas(32) uint32_t lanes[LANES];
    for (int index=0;index<PREFIX_WORDS;++index) {
        __m256i value=state[index];
        value=_mm256_xor_si256(value,_mm256_srli_epi32(value,11));
        value=_mm256_xor_si256(value,_mm256_and_si256(_mm256_slli_epi32(value,7),_mm256_set1_epi32(static_cast<int>(0x9d2c5680U))));
        value=_mm256_xor_si256(value,_mm256_and_si256(_mm256_slli_epi32(value,15),_mm256_set1_epi32(static_cast<int>(0xefc60000U))));
        value=_mm256_xor_si256(value,_mm256_srli_epi32(value,18));
        _mm256_store_si256(reinterpret_cast<__m256i*>(lanes),value);
        for(int lane=0;lane<LANES;++lane) output[index][lane]=lanes[lane];
    }
}

static std::array<uint32_t,3> triplet(PythonMT& engine) {
    std::array<uint32_t,3> values{}; std::size_t count=0;
    while(count<3){uint32_t value=100U+engine.below();if(std::find(values.begin(),values.begin()+count,value)==values.begin()+count)values[count++]=value;}
    return values;
}

static std::array<uint32_t,3> float_triplet(PythonMT& engine) {
    return {
        100U + static_cast<uint32_t>(engine.random53() * 900.0),
        100U + static_cast<uint32_t>(engine.random53() * 900.0),
        100U + static_cast<uint32_t>(engine.random53() * 900.0)
    };
}

// 1=match, 0=definite mismatch, -1=prefix exhausted and scalar fallback needed.
static int prefix_status(const uint32_t raw[PREFIX_WORDS][LANES],int lane,const std::array<uint32_t,3>& expected){
    std::array<uint32_t,3> values{};int cursor=0;std::size_t count=0;
    while(count<3&&cursor<PREFIX_WORDS){uint32_t value=raw[cursor++][lane]>>22;if(value>=900U)continue;value+=100U;if(std::find(values.begin(),values.begin()+count,value)==values.begin()+count)values[count++]=value;}
    if(count!=3)return -1;return values==expected?1:0;
}

static int float_prefix_status(const uint32_t raw[PREFIX_WORDS][LANES],int lane,const std::array<uint32_t,3>& expected){
    for(int index=0;index<3;++index){uint64_t high=raw[index*2][lane]>>5,low=raw[index*2+1][lane]>>6;double value=(high*67108864.0+low)/9007199254740992.0;uint32_t label=100U+static_cast<uint32_t>(value*900.0);if(label!=expected[index])return 0;}return 1;
}

static bool self_test(){
    constexpr std::array<uint32_t,5> seeds{0U,1U,42U,12345U,0xffffffffU};
    constexpr std::array<std::array<uint32_t,3>,5> integer_expected{{{964U,494U,876U},{237U,682U,967U},{754U,214U,125U},{526U,850U,110U},{750U,734U,308U}}};
    constexpr std::array<std::array<uint32_t,3>,5> float_expected{{{859U,782U,478U},{220U,862U,787U},{675U,122U,347U},{474U,109U,842U},{671U,282U,646U}}};
    for(std::size_t i=0;i<seeds.size();++i){PythonMT integer_engine(seeds[i]);if(triplet(integer_engine)!=integer_expected[i])return false;PythonMT float_engine(seeds[i]);if(float_triplet(float_engine)!=float_expected[i])return false;}
    for(uint32_t base:{0U,8U,40U,12344U,0xfffffff8U}){alignas(32) uint32_t words[PREFIX_WORDS][LANES];raw_prefix(base,words);for(int lane=0;lane<LANES;++lane){PythonMT engine(base+lane);for(int i=0;i<PREFIX_WORDS;++i)if(words[i][lane]!=engine.word())return false;}}
    return true;
}

static uint32_t triplet_key(const std::array<uint32_t,3>& values) {
    return values[0] | (values[1] << 10U) | (values[2] << 20U);
}

static std::vector<std::array<uint32_t,3>> load_targets(const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("could not open target file");
    std::vector<std::array<uint32_t,3>> targets;
    std::string line;
    while (std::getline(input, line)) {
        if (line.empty()) continue;
        std::replace(line.begin(), line.end(), ',', ' ');
        std::istringstream values(line);
        std::array<uint32_t,3> target{};
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

static std::array<uint32_t,3> prefix_triplet(const uint32_t raw[PREFIX_WORDS][LANES], int lane) {
    std::array<uint32_t,3> values{};
    int cursor = 0;
    std::size_t count = 0;
    while (count < values.size() && cursor < PREFIX_WORDS) {
        uint32_t value = raw[cursor++][lane] >> 22;
        if (value >= 900U) continue;
        value += 100U;
        if (std::find(values.begin(), values.begin() + count, value) == values.begin() + count) {
            values[count++] = value;
        }
    }
    if (count != values.size()) return {0U, 0U, 0U};
    return values;
}

static std::array<uint32_t,3> prefix_float_triplet(const uint32_t raw[PREFIX_WORDS][LANES], int lane) {
    std::array<uint32_t,3> values{};
    for (int index = 0; index < 3; ++index) {
        const uint64_t high = raw[index * 2][lane] >> 5;
        const uint64_t low = raw[index * 2 + 1][lane] >> 6;
        const double value = (high * 67108864.0 + low) / 9007199254740992.0;
        values[index] = 100U + static_cast<uint32_t>(value * 900.0);
    }
    return values;
}

static int run_multi_target(const std::string& path, uint64_t start, uint64_t stop, int threads) {
    const auto targets = load_targets(path);
    std::unordered_map<uint32_t, std::vector<std::size_t>> target_indices;
    for (std::size_t index = 0; index < targets.size(); ++index) {
        target_indices[triplet_key(targets[index])].push_back(index);
    }
    std::vector<std::vector<uint32_t>> integer_hits(targets.size());
    std::vector<std::vector<uint32_t>> float_hits(targets.size());
    const uint64_t batches = (stop - start + LANES - 1) / LANES;
    std::mutex mutex;
#ifdef _OPENMP
    omp_set_num_threads(threads);
#endif
#pragma omp parallel
    {
        std::vector<std::pair<std::size_t,uint32_t>> local_integer;
        std::vector<std::pair<std::size_t,uint32_t>> local_float;
#pragma omp for schedule(static)
        for (uint64_t batch = 0; batch < batches; ++batch) {
            const uint64_t base64 = start + batch * LANES;
            alignas(32) uint32_t words[PREFIX_WORDS][LANES];
            raw_prefix(static_cast<uint32_t>(base64), words);
            for (int lane = 0; lane < LANES; ++lane) {
                const uint64_t seed64 = base64 + static_cast<uint64_t>(lane);
                if (seed64 >= stop) continue;
                auto integer_values = prefix_triplet(words, lane);
                if (integer_values[0] == 0U) {
                    PythonMT engine(static_cast<uint32_t>(seed64));
                    integer_values = triplet(engine);
                }
                auto found = target_indices.find(triplet_key(integer_values));
                if (found != target_indices.end()) {
                    for (std::size_t target : found->second) {
                        local_integer.emplace_back(target, static_cast<uint32_t>(seed64));
                    }
                }
                const auto float_values = prefix_float_triplet(words, lane);
                found = target_indices.find(triplet_key(float_values));
                if (found != target_indices.end()) {
                    for (std::size_t target : found->second) {
                        local_float.emplace_back(target, static_cast<uint32_t>(seed64));
                    }
                }
            }
        }
        if (!local_integer.empty() || !local_float.empty()) {
            std::lock_guard<std::mutex> lock(mutex);
            for (const auto& [target, seed] : local_integer) integer_hits[target].push_back(seed);
            for (const auto& [target, seed] : local_float) float_hits[target].push_back(seed);
        }
    }
    for (auto& hits : integer_hits) std::sort(hits.begin(), hits.end());
    for (auto& hits : float_hits) std::sort(hits.begin(), hits.end());
    std::cout << "{\"mode\":\"multi-target\",\"start\":" << start
              << ",\"stop_exclusive\":" << stop << ",\"tested\":" << (stop - start)
              << ",\"targets\":[";
    for (std::size_t index = 0; index < targets.size(); ++index) {
        if (index) std::cout << ',';
        std::cout << "{\"target\":[" << targets[index][0] << ',' << targets[index][1] << ',' << targets[index][2]
                  << "],\"integer_candidates\":[";
        for (std::size_t hit = 0; hit < integer_hits[index].size(); ++hit) {
            if (hit) std::cout << ',';
            std::cout << integer_hits[index][hit];
        }
        std::cout << "],\"float_candidates\":[";
        for (std::size_t hit = 0; hit < float_hits[index].size(); ++hit) {
            if (hit) std::cout << ',';
            std::cout << float_hits[index][hit];
        }
        std::cout << "]}";
    }
    std::cout << "]}\n";
    return 0;
}

int main(int argc,char** argv){
    if(argc==2&&std::string(argv[1])=="--self-test"){bool ok=self_test();std::cout<<(ok?"ok":"failed")<<'\n';return ok?0:1;}
    if(argc==6&&std::string(argv[1])=="--multi-target"){
        const uint64_t start=std::stoull(argv[3]),stop=std::stoull(argv[4]);
        if(start>stop||stop>(uint64_t{1}<<32)||(start%LANES))return 2;
        return run_multi_target(argv[2],start,stop,std::max(1,std::stoi(argv[5])));
    }
    bool first_target=argc==5&&std::string(argv[1])=="--first-target";if(!first_target&&(argc<3||argc>4))return 2;int offset=first_target?1:0;uint64_t start=std::stoull(argv[offset+1]),stop=std::stoull(argv[offset+2]);if(start>stop||stop>(uint64_t{1}<<32)||(start%LANES))return 2;
    int threads=(first_target||argc==4)?std::max(1,std::stoi(argv[offset+3])):1;
#ifdef _OPENMP
    omp_set_num_threads(threads);
#endif
    constexpr std::array<uint32_t,3> prelude{654,347,964},first{491,210,379},second{795,975,199};const auto& target=first_target?first:prelude;const auto& following=first_target?second:first;uint64_t batches=(stop-start+LANES-1)/LANES;std::atomic<uint64_t> prelude_hits{0};std::mutex mutex;std::vector<uint32_t> prelude_candidates,hits,float_candidates,float_hits;
#pragma omp parallel
    {std::vector<uint32_t> local_prelude,local,local_float,local_float_hits;
#pragma omp for schedule(static)
    for(uint64_t batch=0;batch<batches;++batch){uint64_t base64=start+batch*LANES;alignas(32) uint32_t words[PREFIX_WORDS][LANES];raw_prefix(static_cast<uint32_t>(base64),words);for(int lane=0;lane<LANES;++lane){uint64_t seed64=base64+lane;if(seed64>=stop)continue;int status=prefix_status(words,lane,target);int float_status=first_target?float_prefix_status(words,lane,target):0;if(status!=0){PythonMT engine(static_cast<uint32_t>(seed64));if(triplet(engine)==target){prelude_hits.fetch_add(1,std::memory_order_relaxed);local_prelude.push_back(static_cast<uint32_t>(seed64));if(triplet(engine)==following)local.push_back(static_cast<uint32_t>(seed64));}}if(float_status!=0){PythonMT engine(static_cast<uint32_t>(seed64));if(float_triplet(engine)==target){local_float.push_back(static_cast<uint32_t>(seed64));if(float_triplet(engine)==following)local_float_hits.push_back(static_cast<uint32_t>(seed64));}}}}
    if(!local.empty()||!local_prelude.empty()||!local_float.empty()||!local_float_hits.empty()){std::lock_guard<std::mutex> lock(mutex);prelude_candidates.insert(prelude_candidates.end(),local_prelude.begin(),local_prelude.end());hits.insert(hits.end(),local.begin(),local.end());float_candidates.insert(float_candidates.end(),local_float.begin(),local_float.end());float_hits.insert(float_hits.end(),local_float_hits.begin(),local_float_hits.end());}}
    std::sort(prelude_candidates.begin(),prelude_candidates.end());std::sort(hits.begin(),hits.end());std::sort(float_candidates.begin(),float_candidates.end());std::sort(float_hits.begin(),float_hits.end());std::cout<<"{\"mode\":\""<<(first_target?"first-target":"prelude")<<"\",\"start\":"<<start<<",\"stop_exclusive\":"<<stop<<",\"tested\":"<<(stop-start)<<",\"prelude_hits\":"<<prelude_hits.load()<<",\"prelude_candidates\":[";for(std::size_t i=0;i<prelude_candidates.size();++i){if(i)std::cout<<',';std::cout<<prelude_candidates[i];}std::cout<<"],\"first_discovery_hits\":[";for(std::size_t i=0;i<hits.size();++i){if(i)std::cout<<',';std::cout<<hits[i];}std::cout<<"],\"float_target_candidates\":[";for(std::size_t i=0;i<float_candidates.size();++i){if(i)std::cout<<',';std::cout<<float_candidates[i];}std::cout<<"],\"float_following_hits\":[";for(std::size_t i=0;i<float_hits.size();++i){if(i)std::cout<<',';std::cout<<float_hits[i];}std::cout<<"]}\n";return 0;
}

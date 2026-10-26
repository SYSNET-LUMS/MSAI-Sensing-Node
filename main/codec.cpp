#include "codec.h"
#include <string.h>
namespace {
struct Bits {
  uint8_t *out; const uint8_t *in; size_t limit,pos=0; bool ok=true;
  void put(uint32_t v,unsigned n) {
    if(pos+n>limit*8) { ok=false; return; }
    for(unsigned i=0;i<n;++i,++pos) out[pos/8]|=((v>>(n-1-i))&1)<<(7-pos%8);
  }
  uint32_t get(unsigned n) {
    if(pos+n>limit*8) { ok=false; return 0; }
    uint32_t v=0; for(unsigned i=0;i<n;++i,++pos) v=(v<<1)|((in[pos/8]>>(7-pos%8))&1);
    return v;
  }
};
uint32_t zig(int32_t x) { return x<0?uint32_t(-int64_t(x)*2-1):uint32_t(x)*2; }
}
size_t msai_encode(const int32_t *s,unsigned n,unsigned channels,uint8_t *out,size_t cap) {
  if(!n||n>128||!channels||channels>8) return 0;
  for(unsigned i=0;i<n*channels;++i) if(s[i]<-8388608||s[i]>8388607) return 0;
  memset(out,0,cap); Bits b{out,nullptr,cap};
  for(unsigned c=0;c<channels;++c) {
    // Cache each difference once rather than recomputing it for all 25 k values.
    uint32_t differences[127];
    for(unsigned i=1;i<n;++i)differences[i-1]=zig(s[i*channels+c]-s[(i-1)*channels+c]);
    // Maximum cost is 127 * (0x1fffffe + 1), which fits in uint32_t.
    unsigned best=24; uint32_t cost=UINT32_MAX;
    for(unsigned k=0;k<=24;++k) {
      uint32_t sum=0;
      for(unsigned i=0;i+1<n;++i){sum+=(differences[i]>>k)+1+k;if(sum>=cost)break;}
      if(sum<cost) { cost=sum; best=k; }
    }
    b.put(best,5); b.put(uint32_t(s[c])&0xffffff,24);
    for(unsigned i=1;i<n;++i) {
      uint32_t z=differences[i-1],q=z>>best;
      for(uint32_t j=0;j<q&&b.ok;++j) b.put(0,1);
      b.put(1,1); b.put(z&((1u<<best)-1),best);
    }
  }
  return b.ok?(b.pos+7)/8:0;
}
bool msai_decode(const uint8_t *data,size_t size,unsigned n,unsigned channels,int32_t *out) {
  if(!n||n>128||!channels||channels>8) return false;
  Bits b{nullptr,data,size};
  for(unsigned c=0;c<channels;++c) {
    unsigned k=b.get(5); if(k>24||!b.ok) return false;
    uint32_t raw=b.get(24); int32_t value=(raw&0x800000)?int32_t(raw|0xff000000):int32_t(raw);
    out[c]=value;
    for(unsigned i=1;i<n;++i) {
      uint32_t q=0;
      while(b.ok&&!b.get(1)) if(++q>(0x1fffffeu>>k)) return false;
      uint32_t z=(q<<k)|b.get(k); if(!b.ok||z>0x1fffffeu) return false;
      int32_t delta=(z&1)?-int32_t((z>>1)+1):int32_t(z>>1);
      int64_t next=int64_t(value)+delta; if(next< -8388608||next>8388607) return false;
      value=int32_t(next); out[i*channels+c]=value;
    }
  }
  if(!b.ok||(b.pos+7)/8!=size) return false;
  while(b.pos<size*8) if(b.get(1)!=0) return false;
  return true;
}
uint32_t msai_crc32(const uint8_t *p,size_t n) {
  uint32_t crc=0xffffffff;
  while(n--) { crc^=*p++; for(int j=0;j<8;++j) crc=(crc>>1)^(0xedb88320u&uint32_t(-int32_t(crc&1))); }
  return ~crc;
}

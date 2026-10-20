<script setup>
import { onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { api } from "../api";
import { money, num, pct } from "../format";
import { regionState } from "../region";
import RegionSelect from "../components/RegionSelect.vue";

const route = useRoute();
const author = ref(null);
const similar = ref([]);
const error = ref("");

async function load() {
  error.value = "";
  try {
    const id = route.params.id;
    author.value = await api(`/influencers/${encodeURIComponent(id)}/estimate`, { query: { region: regionState.value } });
    const sim = await api(`/influencers/${encodeURIComponent(id)}/similar`, { query: { region: regionState.value, limit: 6 } });
    similar.value = sim.items || [];
  } catch (err) { error.value = err.message; }
}
onMounted(load);
watch(() => [route.params.id, regionState.value], load);
</script>

<template>
  <header class="top">
    <div>
      <h1>{{ author?.nickname || "达人" }}</h1>
      <p class="sub">{{ author?.author_id }} · 粉丝 {{ num(author?.fans_count) }}</p>
    </div>
    <RegionSelect />
  </header>
  <p v-if="error" class="error">{{ error }}</p>
  <div v-if="author?.estimate" class="grid" style="gap:16px">
    <div class="grid grid-4">
      <article class="card"><div class="hint">预估阅读</div><div class="metric">{{ num(author.estimate.estimated_reads) }}</div></article>
      <article class="card"><div class="hint">预估曝光</div><div class="metric">{{ num(author.estimate.estimated_exposure) }}</div></article>
      <article class="card"><div class="hint">互动率</div><div class="metric">{{ author.estimate.engagement_rate_pct || pct(author.estimate.engagement_rate) }}</div></article>
      <article class="card"><div class="hint">粉丝质量</div><div class="metric">{{ author.estimate.fan_quality }}</div><div class="hint">可疑 {{ author.estimate.suspicious_fan_score }}</div></article>
    </div>
    <section class="card">
      <h3>报价、CPE、CPM</h3>
      <p>{{ money(author.estimate.quote.low) }} – {{ money(author.estimate.quote.high) }}（中位 {{ money(author.estimate.quote.mid) }}，{{ author.estimate.quote.unit }}）</p>
      <p class="hint">CPE {{ author.estimate.cpe ?? "—" }} · CPM {{ author.estimate.cpm ?? "—" }} · {{ author.estimate.quote.factor_note }}</p>
      <p class="hint">类目 {{ author.estimate.category }} · 节奏 {{ author.estimate.cadence.label }}<span v-if="author.estimate.cadence.median_gap_days">，间隔 {{ author.estimate.cadence.median_gap_days }} 天</span></p>
      <p class="hint">置信度 {{ author.estimate.confidence.label }}（{{ author.estimate.confidence.score }}，样本 {{ author.estimate.confidence.sample_notes }} 篇）</p>
      <p v-for="reason in author.estimate.fan_quality_reasons" :key="reason" class="hint">{{ reason }}</p>
    </section>
    <section class="card formula">
      <h3>公式 {{ author.estimate.formula_version }}</h3>
      <p v-for="(text, key) in author.estimate.formulas" :key="key"><b>{{ key }}</b>　{{ text }}</p>
    </section>
    <section>
      <h3>相似达人</h3>
      <div class="grid grid-2">
        <article v-for="item in similar" :key="item.author_id" class="card">
          <h3>{{ item.nickname || item.author_id }}</h3>
          <p class="hint">{{ item.similar_reason }} · 相似度 {{ item.similarity }}</p>
          <p class="hint">阅读 {{ num(item.estimate?.estimated_reads) }} · 报价 {{ money(item.estimate?.quote?.low) }}–{{ money(item.estimate?.quote?.high) }} · {{ item.estimate?.category }}</p>
        </article>
        <p v-if="!similar.length" class="empty">没有足够接近的达人。</p>
      </div>
    </section>
  </div>
</template>

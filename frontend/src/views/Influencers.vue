<script setup>
import { onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";
import { money, num, pct } from "../format";
import { regionState } from "../region";
import RegionSelect from "../components/RegionSelect.vue";

const router = useRouter();
const items = ref([]);
const formulas = ref(null);
const q = ref("");
const error = ref("");
const showFormula = ref(false);

async function load() {
  error.value = "";
  try {
    const data = await api("/influencers", { query: { q: q.value, region: regionState.value, page_size: 30 } });
    items.value = data.items || [];
    formulas.value = data.formulas || null;
  } catch (err) { error.value = err.message; }
}
onMounted(load);
watch(() => regionState.value, load);

function open(item) {
  router.push(`/influencers/${encodeURIComponent(item.author_id)}`);
}
</script>

<template>
  <header class="top">
    <div>
      <h1>达人预估</h1>
      <p class="sub">阅读、曝光、CPE、CPM 和报价都由已采集笔记自算，公式可展开。</p>
    </div>
    <RegionSelect />
  </header>
  <div class="row" style="margin-bottom:12px">
    <input v-model="q" placeholder="搜索昵称或小红书号" @keyup.enter="load" />
    <button class="btn" type="button" @click="load">搜索</button>
    <button class="btn ghost" type="button" @click="showFormula = !showFormula">{{ showFormula ? "收起公式" : "查看公式" }}</button>
  </div>
  <section v-if="showFormula && formulas" class="card formula" style="margin-bottom:12px">
    <p v-for="(text, key) in formulas" :key="key"><b>{{ key }}</b>　{{ text }}</p>
  </section>
  <p v-if="error" class="error">{{ error }}</p>
  <div class="grid">
    <article v-for="item in items" :key="item.author_id" class="card" style="cursor:pointer" @click="open(item)">
      <div class="row" style="justify-content:space-between">
        <h3>{{ item.nickname || item.author_id }}</h3>
        <span class="tag">{{ item.estimate?.category || "未分类" }}</span>
      </div>
      <p class="hint">粉丝 {{ num(item.fans_count) }} · {{ item.fans_level }} · {{ item.coop_label }}</p>
      <div class="grid grid-4" style="margin-top:8px">
        <div><div class="hint">预估阅读</div><b>{{ num(item.estimate?.estimated_reads) }}</b></div>
        <div><div class="hint">预估曝光</div><b>{{ num(item.estimate?.estimated_exposure) }}</b></div>
        <div><div class="hint">互动率</div><b>{{ item.estimate?.engagement_rate_pct || pct(item.estimate?.engagement_rate) }}</b></div>
        <div><div class="hint">报价</div><b>{{ money(item.estimate?.quote?.low) }}–{{ money(item.estimate?.quote?.high) }}</b></div>
      </div>
      <p class="hint">
        CPE {{ item.estimate?.cpe ?? "—" }} · CPM {{ item.estimate?.cpm ?? "—" }} ·
        粉丝质量 {{ item.estimate?.fan_quality ?? "—" }} ·
        可疑粉丝 {{ item.estimate?.suspicious_fan_score ?? "—" }} ·
        {{ item.estimate?.cadence?.label || "节奏未知" }} ·
        置信度 {{ item.estimate?.confidence?.label }}
      </p>
    </article>
    <p v-if="!items.length" class="empty">达人库是空的。采集笔记后，作者会自动出现在这里。</p>
  </div>
</template>

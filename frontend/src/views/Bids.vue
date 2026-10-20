<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const items = ref([]);
const name = ref("");
const text = ref("");
const compare = ref(null);
const error = ref("");

async function load() {
  items.value = (await api("/bids")).items || (await api("/bids"));
  if (!Array.isArray(items.value)) items.value = items.value.items || [];
}
async function create() {
  error.value = "";
  try {
    await api("/bids/text", { method: "POST", body: { name: name.value, text: text.value } });
    name.value = ""; text.value = "";
    await load();
  } catch (err) { error.value = err.message; }
}
async function runCompare() {
  error.value = "";
  try { compare.value = await api("/bids/compare"); }
  catch (err) { error.value = err.message; }
}
onMounted(load);
</script>

<template>
  <header class="top">
    <div><h1>比稿</h1><p class="sub">粘贴方案正文，规则拆解后可以横向对比。</p></div>
    <button class="btn ghost" type="button" @click="runCompare">对比</button>
  </header>
  <p v-if="error" class="error">{{ error }}</p>
  <section class="card" style="margin-bottom:12px">
    <div class="grid">
      <input v-model="name" placeholder="方案名称" />
      <textarea v-model="text" placeholder="创意概念、报价、KPI" />
      <button class="btn" type="button" @click="create">保存文本方案</button>
    </div>
  </section>
  <ul>
    <li v-for="item in items" :key="item.id">{{ item.name }}</li>
  </ul>
  <pre v-if="compare" class="formula card">{{ JSON.stringify(compare.bids || compare, null, 2).slice(0, 2000) }}</pre>
</template>

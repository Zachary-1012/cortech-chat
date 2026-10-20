<script setup>
import { onMounted, reactive, ref } from "vue";
import { api } from "../api";

const items = ref([]);
const board = ref(null);
const error = ref("");
const form = reactive({ name: "", keywords: "", is_self: false });

async function load() {
  items.value = (await api("/competitors")).items || [];
  board.value = await api("/competitors/dashboard", { query: { days: 30 } });
}
async function create() {
  error.value = "";
  try {
    await api("/competitors", { method: "POST", body: form });
    form.name = ""; form.keywords = "";
    await load();
  } catch (err) { error.value = err.message; }
}
onMounted(load);
</script>

<template>
  <header class="top"><div><h1>竞品</h1><p class="sub">近 30 天声量对比。</p></div></header>
  <p v-if="error" class="error">{{ error }}</p>
  <section class="card" style="margin-bottom:12px">
    <div class="row">
      <input v-model="form.name" placeholder="品牌名" />
      <input v-model="form.keywords" placeholder="关键词" />
      <label class="hint"><input type="checkbox" v-model="form.is_self" /> 本方</label>
      <button class="btn" type="button" @click="create">添加</button>
    </div>
  </section>
  <div class="grid">
    <article v-for="entry in board?.entries || []" :key="entry.name" class="card">
      <h3>{{ entry.name }} <span v-if="entry.is_self" class="tag">本方</span></h3>
      <p class="metric">{{ entry.volume_total }}</p>
      <p class="hint">负面率 {{ Math.round((entry.neg_ratio || 0) * 100) }}%</p>
    </article>
    <p v-if="!board?.entries?.length" class="empty">还没有竞品。</p>
  </div>
</template>

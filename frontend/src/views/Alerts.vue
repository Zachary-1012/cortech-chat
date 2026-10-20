<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";
import { when } from "../format";

const items = ref([]);
const error = ref("");

async function load() {
  const data = await api("/alerts");
  items.value = data.items || [];
}
async function read(item) {
  error.value = "";
  try {
    await api(`/alerts/${item.id}/read`, { method: "POST" });
    await load();
  } catch (err) { error.value = err.message; }
}
onMounted(load);
</script>

<template>
  <header class="top"><div><h1>预警</h1><p class="sub">负面突增、敏感词和声量异常。</p></div></header>
  <p v-if="error" class="error">{{ error }}</p>
  <div class="grid">
    <article v-for="item in items" :key="item.id" class="card">
      <h3>{{ item.title }} <span class="tag" :class="item.level">{{ item.level }}</span></h3>
      <p>{{ item.message }}</p>
      <p class="hint">{{ item.status }} · {{ when(item.created_at) }}</p>
      <button v-if="item.status !== 'read'" class="btn ghost" type="button" @click="read(item)">标为已读</button>
    </article>
    <p v-if="!items.length" class="empty">没有预警。</p>
  </div>
</template>

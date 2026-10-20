<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";
import { when } from "../format";

const items = ref([]);
const sources = ref([]);
const error = ref("");

async function load() {
  error.value = "";
  try {
    items.value = (await api("/news")).items || [];
    sources.value = (await api("/news/sources")).items || [];
  } catch (err) { error.value = err.message; }
}
onMounted(load);
</script>

<template>
  <header class="top"><div><h1>资讯</h1><p class="sub">新闻搜索和 RSS。来源 {{ sources.length }} 个。</p></div></header>
  <p v-if="error" class="error">{{ error }}</p>
  <div class="grid">
    <article v-for="item in items" :key="item.id || item.url" class="card">
      <h3><a :href="item.url" target="_blank" rel="noreferrer">{{ item.title }}</a></h3>
      <p class="hint">{{ item.source }} · {{ when(item.publish_time) }} · {{ item.sentiment }}</p>
      <p>{{ item.summary }}</p>
    </article>
    <p v-if="!items.length" class="empty">还没有资讯。</p>
  </div>
</template>

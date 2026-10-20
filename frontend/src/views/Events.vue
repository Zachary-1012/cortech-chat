<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const topics = ref([]);
const topicId = ref("");
const items = ref([]);
const message = ref("");
const error = ref("");

async function load() {
  const [topicRes, eventRes] = await Promise.all([
    api("/topics"),
    api("/events", { query: { topic_id: topicId.value || undefined } }),
  ]);
  topics.value = topicRes.items || [];
  items.value = eventRes.items || [];
}
async function generate() {
  error.value = "";
  if (!topicId.value) { error.value = "先选择一个话题"; return; }
  try {
    const res = await api("/events/generate", { method: "POST", body: { topic_id: Number(topicId.value), days: 14 } });
    message.value = res.message || "已生成";
    await load();
  } catch (err) { error.value = err.message; }
}
onMounted(load);
</script>

<template>
  <header class="top">
    <div>
      <h1>事件</h1>
      <p class="sub">把近两周帖子聚成事件。</p>
    </div>
    <button class="btn" type="button" @click="generate">重新聚类</button>
  </header>
  <div class="row" style="margin-bottom:12px">
    <select v-model="topicId" @change="load">
      <option value="">全部话题</option>
      <option v-for="topic in topics" :key="topic.id" :value="String(topic.id)">{{ topic.name }}</option>
    </select>
  </div>
  <p v-if="message" class="hint">{{ message }}</p>
  <p v-if="error" class="error">{{ error }}</p>
  <div class="grid">
    <article v-for="event in items" :key="event.id" class="card">
      <h3>{{ event.title }}</h3>
      <p>{{ event.summary }}</p>
      <p class="hint">{{ event.note_count }} 条 · {{ event.sentiment }}</p>
    </article>
    <p v-if="!items.length" class="empty">还没有事件。</p>
  </div>
</template>

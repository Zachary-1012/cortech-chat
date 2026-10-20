<script setup>
import { onMounted, ref, watch } from "vue";
import { api } from "../api";
import { num } from "../format";
import { regionState } from "../region";
import RegionSelect from "../components/RegionSelect.vue";

const topics = ref([]);
const topicId = ref("");
const data = ref(null);
const error = ref("");

async function loadTopics() {
  const res = await api("/topics");
  topics.value = res.items || [];
  if (!topicId.value && topics.value[0]) topicId.value = String(topics.value[0].id);
}
async function load() {
  if (!topicId.value) return;
  error.value = "";
  try {
    data.value = await api("/timeline", { query: { topic_id: topicId.value, region: regionState.value } });
  } catch (err) { error.value = err.message; }
}
onMounted(async () => { await loadTopics(); await load(); });
watch([topicId, () => regionState.value], load);
</script>

<template>
  <header class="top">
    <div>
      <h1>时间线</h1>
      <p class="sub">按发布时间分档。没有发布时间的帖子单独放在「时间未知」。</p>
    </div>
    <RegionSelect />
  </header>
  <div class="row" style="margin-bottom:12px">
    <select v-model="topicId">
      <option v-for="topic in topics" :key="topic.id" :value="String(topic.id)">{{ topic.name }}</option>
    </select>
  </div>
  <p v-if="error" class="error">{{ error }}</p>
  <div v-if="data" class="grid">
    <article v-for="bucket in data.buckets" :key="bucket.key" class="card">
      <h3>{{ bucket.label }} <span class="tag">{{ bucket.count }} 条</span></h3>
      <p class="hint">平均互动 {{ num(bucket.avg_engagement) }} · 中位数 {{ num(bucket.median_engagement) }}</p>
      <p v-for="post in (bucket.posts || []).slice(0, 4)" :key="post.post_id" class="hint">{{ post.title }}</p>
    </article>
    <p v-if="!data.buckets?.length" class="empty">这个话题在当前地区没有可排进时间线的帖子。</p>
  </div>
</template>

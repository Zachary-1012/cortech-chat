<script setup>
import { onMounted, ref, watch } from "vue";
import { api } from "../api";
import { regionState } from "../region";
import RegionSelect from "../components/RegionSelect.vue";

const topics = ref([]);
const topicId = ref("");
const words = ref([]);
const detail = ref(null);
const error = ref("");

async function load() {
  error.value = "";
  try {
    const topicsRes = await api("/topics");
    topics.value = topicsRes.items || [];
    const trends = await api("/trends", {
      query: { days: 30, topic_id: topicId.value || undefined },
    });
    words.value = trends.hot_words || trends.words || [];
  } catch (err) { error.value = err.message; }
}
async function openWord(word) {
  const text = word.word || word.name || word;
  detail.value = await api("/hot-words/detail", { query: { word: text, topic_id: topicId.value || undefined } });
}
onMounted(load);
watch(topicId, load);
</script>

<template>
  <header class="top">
    <div>
      <h1>热词</h1>
      <p class="sub">近 30 天总量榜。点一个词看关联品牌。地区筛选作用在总览和帖子，热词沿用话题口径。</p>
    </div>
    <RegionSelect />
  </header>
  <div class="row" style="margin-bottom:12px">
    <select v-model="topicId">
      <option value="">全部话题</option>
      <option v-for="topic in topics" :key="topic.id" :value="String(topic.id)">{{ topic.name }}</option>
    </select>
  </div>
  <p v-if="error" class="error">{{ error }}</p>
  <p class="hint" v-if="regionState.value">当前地区偏好：{{ regionState.value }}。热词接口按话题统计。</p>
  <section class="card">
    <div class="chips">
      <button v-for="(word, idx) in words.slice(0, 40)" :key="idx" class="chip" type="button" @click="openWord(word)">
        {{ word.word || word.name || word }} {{ word.count || "" }}
      </button>
      <p v-if="!words.length" class="empty">还没有热词。</p>
    </div>
    <div v-if="detail" style="margin-top:12px">
      <h3>{{ detail.word }}</h3>
      <p v-for="item in (detail.brands || detail.items || []).slice(0, 8)" :key="item.name || item.brand" class="hint">
        {{ item.name || item.brand || item }}
      </p>
    </div>
  </section>
</template>

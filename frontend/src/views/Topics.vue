<script setup>
import { onMounted, reactive, ref, watch } from "vue";
import { api } from "../api";
import { regionState } from "../region";

const items = ref([]);
const error = ref("");
const expanded = ref([]);
const form = reactive({
  name: "", keywords: "", max_notes: 50, enable_comments: 1, region: "cn", engine: "cortech_direct",
});

async function load() {
  const data = await api("/topics");
  items.value = data.items || [];
}
async function preview() {
  if (!form.keywords.trim()) { expanded.value = []; return; }
  const data = await api("/xhs/keywords/expand", { query: { q: form.keywords, region: form.region } });
  expanded.value = data.keywords || [];
}
async function createTopic() {
  error.value = "";
  try {
    await api("/topics", { method: "POST", body: { ...form, enabled: 0, cron: "0 2 * * *" } });
    form.name = ""; form.keywords = "";
    expanded.value = [];
    await load();
  } catch (err) { error.value = err.message; }
}
async function crawl(topic) {
  error.value = "";
  try {
    await api(`/topics/${topic.id}/crawl`, { method: "POST", body: {} });
    error.value = `已启动「${topic.name}」的采集。没有真实登录态时会停在需要扫码。`;
  } catch (err) { error.value = err.message; }
}
async function remove(topic) {
  await api(`/topics/${topic.id}`, { method: "DELETE" });
  await load();
}
onMounted(load);
watch(() => form.keywords, preview);
watch(() => form.region, preview);
watch(() => regionState.value, (value) => { if (value) form.region = value; });
</script>

<template>
  <header class="top">
    <div>
      <h1>话题</h1>
      <p class="sub">选择地区后，采集会换主机和语言头，并用 OpenCC 把关键词扩成简体和繁体。</p>
    </div>
  </header>
  <p v-if="error" class="error">{{ error }}</p>
  <section class="card" style="margin-bottom:16px">
    <div class="grid grid-2">
      <label class="field">名称<input v-model="form.name" /></label>
      <label class="field">地区
        <select v-model="form.region">
          <option value="cn">大陆</option>
          <option value="hk">香港</option>
          <option value="overseas">海外 RED</option>
        </select>
      </label>
      <label class="field">关键词，逗号分隔<input v-model="form.keywords" placeholder="广州, 软件" /></label>
      <label class="field">最多笔记<input v-model.number="form.max_notes" type="number" min="1" max="500" /></label>
    </div>
    <p class="hint" v-if="expanded.length">将搜索：{{ expanded.join("、") }}</p>
    <div class="row" style="margin-top:10px">
      <button class="btn" type="button" @click="createTopic">添加话题</button>
    </div>
  </section>
  <section class="card">
    <table>
      <thead><tr><th>话题</th><th>地区</th><th>关键词</th><th></th></tr></thead>
      <tbody>
        <tr v-for="topic in items" :key="topic.id">
          <td>{{ topic.name }}</td>
          <td><span class="tag">{{ topic.region || "cn" }}</span></td>
          <td>{{ topic.keywords }}</td>
          <td class="row">
            <button class="btn" type="button" @click="crawl(topic)">采集</button>
            <button class="btn ghost" type="button" @click="remove(topic)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-if="!items.length" class="empty">还没有话题。</p>
  </section>
</template>

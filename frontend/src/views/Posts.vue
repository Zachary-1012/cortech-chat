<script setup>
import { onMounted, ref, watch } from "vue";
import { api } from "../api";
import { num, when } from "../format";
import { regionState } from "../region";
import RegionSelect from "../components/RegionSelect.vue";

const items = ref([]);
const total = ref(0);
const q = ref("");
const sentiment = ref("");
const error = ref("");

async function load() {
  error.value = "";
  try {
    const data = await api("/posts", {
      query: { q: q.value, sentiment: sentiment.value, region: regionState.value, page_size: 30 },
    });
    items.value = data.items || [];
    total.value = data.total || 0;
  } catch (err) { error.value = err.message; }
}
onMounted(load);
watch(() => regionState.value, load);
</script>

<template>
  <header class="top">
    <div>
      <h1>帖子</h1>
      <p class="sub">共 {{ total }} 条。发布时间来自笔记时间戳，采集时间是入库时间。</p>
    </div>
    <RegionSelect />
  </header>
  <div class="row" style="margin-bottom:12px">
    <input v-model="q" placeholder="搜索标题、正文、作者" @keyup.enter="load" />
    <select v-model="sentiment" @change="load">
      <option value="">全部情感</option>
      <option value="positive">正面</option>
      <option value="neutral">中性</option>
      <option value="negative">负面</option>
    </select>
    <button class="btn" type="button" @click="load">筛选</button>
  </div>
  <p v-if="error" class="error">{{ error }}</p>
  <section class="card">
    <table>
      <thead><tr><th>标题</th><th>作者</th><th>地区</th><th>赞/藏/评</th><th>发布时间</th></tr></thead>
      <tbody>
        <tr v-for="post in items" :key="post.post_id">
          <td>{{ post.title || "（无标题）" }}<div class="hint">{{ post.post_status === "deleted" ? "已删除" : post.sentiment }}</div></td>
          <td>{{ post.author }}</td>
          <td>{{ post.region || "cn" }}<div class="hint">{{ post.content_script }}</div></td>
          <td>{{ num(post.liked_count) }} / {{ num(post.collected_count) }} / {{ num(post.comment_count) }}</td>
          <td>{{ when(post.publish_time) }}<div class="hint">采集 {{ when(post.collected_at) }}</div></td>
        </tr>
      </tbody>
    </table>
    <p v-if="!items.length" class="empty">没有帖子。</p>
  </section>
</template>

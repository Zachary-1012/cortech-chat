<script setup>
import { onMounted, ref, watch } from "vue";
import { api } from "../api";
import { num } from "../format";
import { regionState } from "../region";
import RegionSelect from "../components/RegionSelect.vue";

const stats = ref(null);
const error = ref("");

async function load() {
  error.value = "";
  try {
    stats.value = await api("/stats", { query: { region: regionState.value } });
  } catch (err) {
    error.value = err.message;
  }
}
onMounted(load);
watch(() => regionState.value, load);
</script>

<template>
  <header class="top">
    <div>
      <h1>总览</h1>
      <p class="sub">声量按发布时间统计。采集时间和发布时间分开存放。</p>
    </div>
    <RegionSelect />
  </header>
  <p v-if="error" class="error">{{ error }}</p>
  <div v-if="stats" class="grid" style="gap:16px">
    <div class="grid grid-4">
      <article class="card"><div class="hint">帖子</div><div class="metric">{{ num(stats.total_notes) }}</div></article>
      <article class="card"><div class="hint">有效帖</div><div class="metric">{{ num(stats.active_notes) }}</div></article>
      <article class="card"><div class="hint">已删除</div><div class="metric">{{ num(stats.deleted_notes) }}</div></article>
      <article class="card"><div class="hint">评论</div><div class="metric">{{ num(stats.total_comments) }}</div></article>
    </div>
    <section class="card">
      <h3>情感分布</h3>
      <div class="bars" v-if="stats.total_notes">
        <span class="pos" :style="{ width: `${100 * stats.sentiment_dist.positive / stats.total_notes}%` }" />
        <span class="neu" :style="{ width: `${100 * stats.sentiment_dist.neutral / stats.total_notes}%` }" />
        <span class="neg" :style="{ width: `${100 * stats.sentiment_dist.negative / stats.total_notes}%` }" />
      </div>
      <p class="hint">正面 {{ stats.sentiment_dist.positive }} · 中性 {{ stats.sentiment_dist.neutral }} · 负面 {{ stats.sentiment_dist.negative }}</p>
    </section>
    <section class="card">
      <h3>地区</h3>
      <div class="chips">
        <span v-for="item in stats.by_region" :key="item.region" class="chip">
          {{ item.label }} {{ item.count }}
        </span>
        <span v-if="!stats.by_region?.length" class="hint">还没有带地区标记的笔记。</span>
      </div>
      <p class="hint">网页端未返回分享数的笔记：{{ stats.share_not_on_web }}。分享数不计入互动。</p>
    </section>
    <section class="card">
      <h3>负面帖</h3>
      <table v-if="stats.negative_top10?.length">
        <thead><tr><th>标题</th><th>作者</th><th>赞</th><th>评</th></tr></thead>
        <tbody>
          <tr v-for="post in stats.negative_top10" :key="post.post_id">
            <td>{{ post.title || "（无标题）" }}</td>
            <td>{{ post.author }}</td>
            <td>{{ num(post.liked_count) }}</td>
            <td>{{ num(post.comment_count) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty">这个筛选下没有负面帖。</p>
    </section>
  </div>
</template>

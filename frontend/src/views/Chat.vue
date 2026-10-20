<script setup>
import { ref } from "vue";
import { api } from "../api";

const message = ref("");
const lines = ref([]);
const error = ref("");

async function send() {
  const text = message.value.trim();
  if (!text) return;
  error.value = "";
  lines.value.push({ role: "user", text });
  message.value = "";
  try {
    const res = await api("/chat", { method: "POST", body: { message: text } });
    lines.value.push({ role: "assistant", text: res.reply || "" });
  } catch (err) { error.value = err.message; }
}
</script>

<template>
  <header class="top"><div><h1>对话</h1><p class="sub">问采集、简报和预警。没有可用模型时用规则回答。</p></div></header>
  <p v-if="error" class="error">{{ error }}</p>
  <section class="card" style="min-height:320px">
    <p v-for="(line, idx) in lines" :key="idx" :class="line.role === 'user' ? '' : 'hint'">
      <b>{{ line.role === "user" ? "我" : "Cortech" }}</b>　{{ line.text }}
    </p>
    <p v-if="!lines.length" class="empty">试着问「列出话题」。</p>
  </section>
  <form class="row" style="margin-top:12px" @submit.prevent="send">
    <input v-model="message" placeholder="输入问题" style="flex:1" />
    <button class="btn" type="submit">发送</button>
  </form>
</template>

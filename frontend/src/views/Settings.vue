<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const setup = ref(null);
const ai = ref(null);
const site = ref(null);
const regions = ref([]);
const error = ref("");

onMounted(async () => {
  try {
    setup.value = await api("/setup/status");
    ai.value = await api("/ai-config");
    site.value = await api("/site-config");
    regions.value = (await api("/xhs/regions")).items || [];
  } catch (err) { error.value = err.message; }
});
</script>

<template>
  <header class="top"><div><h1>设置</h1><p class="sub">运行状态、模型配置和地区主机。</p></div></header>
  <p v-if="error" class="error">{{ error }}</p>
  <div class="grid">
    <section class="card" v-if="setup">
      <h3>运行状态</h3>
      <p v-for="check in setup.checks || []" :key="check.id" class="hint">{{ check.label || check.id }}：{{ check.ok ? "正常" : "未就绪" }} {{ check.detail || "" }}</p>
    </section>
    <section class="card">
      <h3>地区主机</h3>
      <article v-for="item in regions" :key="item.id" class="hint">
        <b>{{ item.label }}</b> 网页 {{ item.web_origin }} · API {{ item.api_origin }} · {{ item.accept_language }}
        <br />{{ item.summary }}
      </article>
    </section>
    <section class="card" v-if="ai">
      <h3>模型</h3>
      <p class="hint">当前 {{ ai.active || "未设置" }}</p>
    </section>
    <section class="card" v-if="site">
      <h3>站点</h3>
      <p class="hint">{{ site.domain || site.brand_name || "尚未绑定域名" }}</p>
    </section>
  </div>
</template>

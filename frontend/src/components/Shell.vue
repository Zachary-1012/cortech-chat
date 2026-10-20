<script setup>
import { useRouter } from "vue-router";
import { api } from "../api";

const router = useRouter();
const links = [
  ["/", "总览"],
  ["/topics", "话题"],
  ["/posts", "帖子"],
  ["/timeline", "时间线"],
  ["/hotwords", "热词"],
  ["/events", "事件"],
  ["/influencers", "达人"],
  ["/competitors", "竞品"],
  ["/alerts", "预警"],
  ["/fakecheck", "伪冒反查"],
  ["/news", "资讯"],
  ["/creator", "创作"],
  ["/bids", "比稿"],
  ["/chat", "对话"],
  ["/accounts", "账号"],
  ["/settings", "设置"],
];

async function logout() {
  await api("/auth/logout", { method: "POST" });
  router.push("/login");
}
</script>

<template>
  <div class="shell">
    <aside class="side">
      <div class="brand">
        <div class="mark">C</div>
        <div>
          <b>Cortech Chat</b>
          <span>采集 · 运营 · 整合营销</span>
        </div>
      </div>
      <nav class="nav">
        <router-link v-for="[href, label] in links" :key="href" :to="href">{{ label }}</router-link>
      </nav>
      <div class="side-foot">
        <button class="btn ghost" style="color:#f6efe6;border-color:#5c4d40" @click="logout">退出登录</button>
      </div>
    </aside>
    <main class="main">
      <router-view />
    </main>
  </div>
</template>

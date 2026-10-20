<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const items = ref([]);
const error = ref("");
const message = ref("");

async function load() {
  const data = await api("/accounts");
  items.value = data.items || [];
}
async function add() {
  error.value = "";
  try {
    const res = await api("/accounts", { method: "POST" });
    message.value = res.message || "已创建账号，请在弹窗或账号状态里完成扫码。服务端不会代登录。";
    await load();
  } catch (err) { error.value = err.message; }
}
async function remove(item) {
  await api(`/accounts/${item.id}`, { method: "DELETE" });
  await load();
}
onMounted(load);
</script>

<template>
  <header class="top">
    <div>
      <h1>账号</h1>
      <p class="sub">只使用你自己扫码或插件确认过的小红书账号。采集是只读的，并受每日配额和健康检查限制。</p>
    </div>
    <button class="btn" type="button" @click="add">添加账号</button>
  </header>
  <p v-if="message" class="hint">{{ message }}</p>
  <p v-if="error" class="error">{{ error }}</p>
  <section class="card">
    <table>
      <thead><tr><th>昵称</th><th>状态</th><th>说明</th><th></th></tr></thead>
      <tbody>
        <tr v-for="item in items" :key="item.id">
          <td>{{ item.nickname || "未命名" }}</td>
          <td>{{ item.state_label || item.status }}</td>
          <td class="hint">{{ item.check_note }}</td>
          <td><button class="btn ghost" type="button" @click="remove(item)">删除</button></td>
        </tr>
      </tbody>
    </table>
    <p v-if="!items.length" class="empty">还没有小红书账号。</p>
  </section>
</template>

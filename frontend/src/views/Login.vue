<script setup>
import { onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";

const route = useRoute();
const router = useRouter();
const mode = ref("login");
const error = ref("");
const form = reactive({ email: "", password: "" });

onMounted(async () => {
  try {
    await api("/auth/me");
    router.replace(route.query.redirect || "/");
  } catch {
    /* 未登录 */
  }
});

async function submit() {
  error.value = "";
  try {
    const path = mode.value === "login" ? "/auth/login" : "/auth/register";
    await api(path, { method: "POST", body: form });
    router.replace(route.query.redirect || "/");
  } catch (err) {
    error.value = err.message;
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="card login-card" @submit.prevent="submit">
      <h1>Cortech Chat</h1>
      <p class="sub">小红书高强采集、社媒运营和整合营销。</p>
      <div class="grid" style="margin-top:16px">
        <label class="field">邮箱
          <input v-model="form.email" type="email" required autocomplete="username" />
        </label>
        <label class="field">密码
          <input v-model="form.password" type="password" required minlength="6" autocomplete="current-password" />
        </label>
        <p v-if="error" class="error">{{ error }}</p>
        <button class="btn" type="submit">{{ mode === "login" ? "登录" : "注册" }}</button>
        <button class="btn ghost" type="button" @click="mode = mode === 'login' ? 'register' : 'login'">
          {{ mode === "login" ? "没有账号，去注册" : "已有账号，去登录" }}
        </button>
      </div>
    </form>
  </div>
</template>

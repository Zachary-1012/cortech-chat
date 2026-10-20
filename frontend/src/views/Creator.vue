<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { api } from "../api";

const types = ref([]);
const typeId = ref("");
const inputs = reactive({});
const content = ref("");
const error = ref("");
const current = computed(() => types.value.find((item) => item.id === typeId.value));

async function load() {
  types.value = (await api("/creator/types")).items || [];
  if (!typeId.value && types.value[0]) typeId.value = types.value[0].id;
}
async function generate() {
  error.value = "";
  try {
    const res = await api("/creator/generate", { method: "POST", body: { type: typeId.value, inputs: { ...inputs } } });
    content.value = res.content || "";
  } catch (err) { error.value = err.message; }
}
onMounted(load);
</script>

<template>
  <header class="top"><div><h1>创作</h1><p class="sub">图文、短视频、活动、海报、直播和评论话术。没有模型时走规则稿。</p></div></header>
  <p v-if="error" class="error">{{ error }}</p>
  <section class="card">
    <label class="field">类型
      <select v-model="typeId">
        <option v-for="item in types" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
    </label>
    <p class="hint">{{ current?.desc }}</p>
    <div class="grid">
      <label v-for="field in current?.fields || []" :key="field.key" class="field">{{ field.label }}
        <textarea v-if="field.type === 'textarea'" v-model="inputs[field.key]" :placeholder="field.placeholder || ''" />
        <select v-else-if="field.type === 'select'" v-model="inputs[field.key]">
          <option v-for="opt in field.options" :key="opt" :value="opt">{{ opt }}</option>
        </select>
        <input v-else v-model="inputs[field.key]" :placeholder="field.placeholder || ''" />
      </label>
    </div>
    <button class="btn" type="button" style="margin-top:10px" @click="generate">生成</button>
    <pre v-if="content" class="formula">{{ content }}</pre>
  </section>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const brands = ref([]);
const brandId = ref("");
const suspects = ref([]);
const name = ref("");
const lookup = ref(null);
const error = ref("");
const brandName = ref("");

async function loadBrands() {
  brands.value = (await api("/fakecheck/brands")).items || [];
  if (!brandId.value && brands.value[0]) brandId.value = String(brands.value[0].id);
  await loadSuspects();
}
async function loadSuspects() {
  if (!brandId.value) { suspects.value = []; return; }
  const data = await api(`/fakecheck/brands/${brandId.value}/suspects`);
  suspects.value = data.items || [];
}
async function addBrand() {
  error.value = "";
  try {
    await api("/fakecheck/brands", { method: "POST", body: { name: brandName.value } });
    brandName.value = "";
    await loadBrands();
  } catch (err) { error.value = err.message; }
}
async function runLookup() {
  error.value = "";
  lookup.value = null;
  try {
    lookup.value = await api("/fakecheck/qcc-lookup", { query: { name: name.value } });
  } catch (err) { error.value = err.message; }
}
onMounted(loadBrands);
</script>

<template>
  <header class="top">
    <div>
      <h1>伪冒反查</h1>
      <p class="sub">主体核验走国家标准校验码、Wikidata、信用中国和公示系统公开页。打不开就降级，不调用付费接口。</p>
    </div>
  </header>
  <p v-if="error" class="error">{{ error }}</p>
  <section class="card" style="margin-bottom:12px">
    <h3>核验主体</h3>
    <div class="row">
      <input v-model="name" placeholder="企业名称或统一社会信用代码" style="min-width:280px" />
      <button class="btn" type="button" @click="runLookup">公开核验</button>
    </div>
    <div v-if="lookup" style="margin-top:10px">
      <p>{{ lookup.message }}</p>
      <p class="hint">结果 {{ lookup.ok ? "有记录" : "未核到" }} · {{ lookup.degraded ? "有来源降级" : "来源正常" }}</p>
      <p v-for="source in lookup.sources" :key="source.id" class="hint">{{ source.title }}：{{ source.status }} · {{ source.detail }}</p>
      <article v-for="(match, idx) in lookup.matches" :key="idx" class="hint">
        {{ match.name || "（仅代码）" }} {{ match.credit_code }} {{ match.reg_address }} · {{ match.source }} · 置信 {{ match.confidence }}
      </article>
    </div>
  </section>
  <section class="card">
    <div class="row" style="margin-bottom:8px">
      <input v-model="brandName" placeholder="品牌名称" />
      <button class="btn ghost" type="button" @click="addBrand">添加品牌</button>
      <select v-model="brandId" @change="loadSuspects">
        <option v-for="brand in brands" :key="brand.id" :value="String(brand.id)">{{ brand.name }}</option>
      </select>
    </div>
    <table>
      <thead><tr><th>昵称</th><th>主体</th><th>信用代码</th><th>等级</th></tr></thead>
      <tbody>
        <tr v-for="item in suspects" :key="item.id">
          <td>{{ item.nickname }}</td>
          <td>{{ item.subject }}</td>
          <td>{{ item.credit_code }}</td>
          <td>{{ item.level }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!suspects.length" class="empty">这个品牌下没有嫌疑账号。</p>
  </section>
</template>

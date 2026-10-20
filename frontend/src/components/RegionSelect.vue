<script setup>
import { REGION_OPTIONS } from "../format";
import { regionState, setRegion } from "../region";

defineProps({
  allowAll: { type: Boolean, default: true },
});
</script>

<template>
  <div class="chips" role="tablist" aria-label="地区">
    <button
      v-for="opt in REGION_OPTIONS.filter((item) => allowAll || item.id)"
      :key="opt.id || 'all'"
      class="chip"
      :class="{ on: regionState.value === opt.id }"
      type="button"
      @click="setRegion(opt.id)"
    >{{ opt.label }}</button>
  </div>
</template>

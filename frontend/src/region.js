import { reactive } from "vue";

const KEY = "cortech.region";

export const regionState = reactive({
  value: localStorage.getItem(KEY) || "",
});

export function setRegion(value) {
  regionState.value = value || "";
  localStorage.setItem(KEY, regionState.value);
}

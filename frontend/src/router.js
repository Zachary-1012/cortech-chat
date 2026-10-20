import { createRouter, createWebHistory } from "vue-router";
import Shell from "./components/Shell.vue";

const child = (path, name, component, title) => ({
  path, name, component, meta: { title },
});

export const router = createRouter({
  history: createWebHistory("/"),
  routes: [
    { path: "/login", name: "login", component: () => import("./views/Login.vue"), meta: { title: "登录" } },
    {
      path: "/",
      component: Shell,
      children: [
        child("", "overview", () => import("./views/Overview.vue"), "总览"),
        child("topics", "topics", () => import("./views/Topics.vue"), "话题"),
        child("posts", "posts", () => import("./views/Posts.vue"), "帖子"),
        child("timeline", "timeline", () => import("./views/Timeline.vue"), "时间线"),
        child("hotwords", "hotwords", () => import("./views/Hotwords.vue"), "热词"),
        child("events", "events", () => import("./views/Events.vue"), "事件"),
        child("influencers", "influencers", () => import("./views/Influencers.vue"), "达人"),
        child("influencers/:id", "influencer", () => import("./views/InfluencerDetail.vue"), "达人预估"),
        child("competitors", "competitors", () => import("./views/Competitors.vue"), "竞品"),
        child("alerts", "alerts", () => import("./views/Alerts.vue"), "预警"),
        child("fakecheck", "fakecheck", () => import("./views/Fakecheck.vue"), "伪冒反查"),
        child("news", "news", () => import("./views/News.vue"), "资讯"),
        child("creator", "creator", () => import("./views/Creator.vue"), "创作"),
        child("bids", "bids", () => import("./views/Bids.vue"), "比稿"),
        child("chat", "chat", () => import("./views/Chat.vue"), "对话"),
        child("settings", "settings", () => import("./views/Settings.vue"), "设置"),
        child("accounts", "accounts", () => import("./views/Accounts.vue"), "账号"),
      ],
    },
  ],
});

router.afterEach((to) => {
  document.title = `${to.meta.title || "Cortech Chat"} · Cortech Chat`;
});

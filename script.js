// Fake "players online" counter that fluctuates a bit for life.
(function () {
  const counter = document.getElementById("onlineCounter");
  let base = 240 + Math.floor(Math.random() * 60);

  function render() {
    base += Math.floor(Math.random() * 7) - 3;
    if (base < 180) base = 180;
    counter.textContent = `🟢 ${base} игроков онлайн`;
  }

  render();
  setInterval(render, 3000);
})();

// Copy server IP to clipboard.
(function () {
  const copyBtn = document.getElementById("copyBtn");
  const original = copyBtn.textContent;

  copyBtn.addEventListener("click", async () => {
    const ip = copyBtn.dataset.ip;
    try {
      await navigator.clipboard.writeText(ip);
      copyBtn.textContent = "IP скопирован!";
    } catch {
      copyBtn.textContent = ip;
    }
    setTimeout(() => (copyBtn.textContent = original), 1500);
  });
})();

// "Начать игру" smooth-scrolls to the join section.
(function () {
  const playBtn = document.getElementById("playBtn");
  playBtn.addEventListener("click", () => {
    document.getElementById("join").scrollIntoView({ behavior: "smooth" });
  });
})();

// Smooth scroll for nav links.
document.querySelectorAll('.nav__links a').forEach((link) => {
  link.addEventListener("click", (e) => {
    const target = document.querySelector(link.getAttribute("href"));
    if (target) {
      e.preventDefault();
      target.scrollIntoView({ behavior: "smooth" });
    }
  });
});

// Mode tiles give a small feedback alert.
document.querySelectorAll(".mode").forEach((tile) => {
  tile.addEventListener("click", () => {
    const names = {
      survival: "Survival",
      creative: "Creative",
      skyblock: "SkyBlock",
    };
    const mode = names[tile.dataset.mode] || "режим";
    alert(`Режим «${mode}» выбран! Заходи на play.craftverse.net`);
  });
});

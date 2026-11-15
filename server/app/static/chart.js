// Hover tooltips for the activity charts. The charts themselves are drawn by the server;
// without this script each bar still has a native title tooltip and the table view.
document.querySelectorAll(".chart").forEach(function (box) {
  const tip = box.querySelector(".tip");
  if (!tip) return;
  const value = tip.querySelector("b");
  const label = tip.querySelector("span");
  let lit = null;

  function clear() {
    tip.hidden = true;
    if (lit) lit.classList.remove("lit");
    lit = null;
  }

  box.addEventListener("pointermove", function (ev) {
    const hit = ev.target.closest(".hit");
    if (!hit) return clear();
    value.textContent = hit.dataset.value;
    label.textContent = hit.dataset.label;
    if (lit) lit.classList.remove("lit");
    lit = box.querySelector('.bar[data-i="' + hit.dataset.i + '"]');
    if (lit) lit.classList.add("lit");
    tip.hidden = false;
    const area = box.getBoundingClientRect();
    const x = ev.clientX - area.left, y = ev.clientY - area.top;
    const left = x + 14 + tip.offsetWidth > area.width ? x - 14 - tip.offsetWidth : x + 14;
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = Math.max(0, y - tip.offsetHeight - 10) + "px";
  });
  box.addEventListener("pointerleave", clear);
});

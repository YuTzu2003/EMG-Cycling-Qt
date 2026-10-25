(() => {
  const search = document.getElementById("recent-search");
  const source = document.getElementById("recent-source");
  const count = document.getElementById("recent-count");
  const empty = document.getElementById("recent-filter-empty");
  const cards = Array.from(document.querySelectorAll("#recent-record-list .dashboard-record-card"));

  if (!search || !source || !count || !empty || !cards.length) return;

  const update = () => {
    const query = search.value.trim().normalize("NFKC").toLocaleLowerCase();
    let visible = 0;
    for (const card of cards) {
      const text = (card.dataset.search || "").normalize("NFKC").toLocaleLowerCase();
      const matches = (!query || text.includes(query)) && (!source.value || card.dataset.source === source.value);
      card.hidden = !matches;
      if (matches) visible += 1;
    }
    count.textContent = `顯示 ${visible} / ${cards.length} 筆`;
    empty.hidden = visible !== 0;
  };

  search.addEventListener("input", update);
  source.addEventListener("change", update);
})();

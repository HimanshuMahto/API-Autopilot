// Small enhancements only. The page reads fine with JavaScript turned off.
(function () {
  // The "js" class is set by a one-line script in <head>, before the page is drawn.

  // Mobile menu
  var toggle = document.querySelector(".nav-toggle");
  var links = document.getElementById("nav-links");
  if (toggle && links) {
    var setOpen = function (open) {
      links.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      toggle.querySelector("use").setAttribute("href", open ? "#i-x" : "#i-list");
    };
    toggle.addEventListener("click", function () {
      setOpen(!links.classList.contains("open"));
    });
    links.addEventListener("click", function (event) {
      if (event.target.closest("a")) setOpen(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") setOpen(false);
    });
  }

  // Copy the commands (without the comment lines)
  document.querySelectorAll(".copy").forEach(function (button) {
    button.addEventListener("click", function () {
      var source = document.getElementById(button.dataset.copy);
      if (!source || !navigator.clipboard) return;
      var text = source.innerText
        .split("\n")
        .filter(function (line) { return line.trim() && line.trim().charAt(0) !== "#"; })
        .join("\n");
      navigator.clipboard.writeText(text).then(function () {
        var label = button.querySelector("span");
        button.classList.add("done");
        label.textContent = "Copied";
        setTimeout(function () {
          button.classList.remove("done");
          label.textContent = "Copy";
        }, 1800);
      });
    });
  });

  // Fade sections in as they scroll into view
  var items = document.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window)) {
    items.forEach(function (el) { el.classList.add("in"); });
    return;
  }
  var observer = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "0px 0px -8% 0px" }
  );
  items.forEach(function (el, index) {
    el.style.transitionDelay = (index % 4) * 70 + "ms";
    observer.observe(el);
  });
})();

// GMCP/MSDP-driven status gauges. Rendered as role=meter with aria-valuetext so a
// screen reader reads "Health 412 of 500" on demand (not announced live).

export class Status {
  constructor(element) {
    this.element = element;
  }

  update(gauges) {
    this.element.innerHTML = "";
    for (const [name, value] of Object.entries(gauges || {})) {
      const meter = document.createElement("div");
      meter.setAttribute("role", "meter");
      meter.setAttribute("aria-label", name);
      if (value && typeof value === "object" && "cur" in value) {
        const max = value.max ?? value.cur;
        meter.setAttribute("aria-valuenow", value.cur);
        meter.setAttribute("aria-valuemin", value.min ?? 0);
        meter.setAttribute("aria-valuemax", max);
        meter.setAttribute("aria-valuetext", `${name} ${value.cur} of ${max}`);
        meter.textContent = `${name}: ${value.cur}/${max}`;
      } else {
        meter.setAttribute("aria-valuetext", `${name} ${value}`);
        meter.textContent = `${name}: ${value}`;
      }
      this.element.appendChild(meter);
    }
  }
}

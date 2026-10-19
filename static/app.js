// Small helpers: mobile sidebar, tabs, and showing/hiding fields.
document.addEventListener("DOMContentLoaded", function () {
    // Open and close the sidebar on small screens
    const sidebar = document.getElementById("sidebar");
    const menuButton = document.getElementById("menu-button");
    if (sidebar && menuButton) {
        menuButton.addEventListener("click", function (event) {
            event.stopPropagation();           // so the "tap outside" handler below does not close it again
            sidebar.classList.toggle("open");
        });
        document.querySelector(".main").addEventListener("click", () => sidebar.classList.remove("open"));
    }

    // Tabs: <div data-tabs> with buttons [data-tab="panel-id"]
    document.querySelectorAll("[data-tabs]").forEach(function (tabBar) {
        tabBar.querySelectorAll("[data-tab]").forEach(function (button) {
            button.addEventListener("click", function () {
                const scope = tabBar.parentElement;
                tabBar.querySelectorAll("[data-tab]").forEach(b => b.classList.remove("active"));
                scope.querySelectorAll(".tab-panel, .room-pane").forEach(p => p.classList.remove("active"));
                button.classList.add("active");
                document.getElementById(button.dataset.tab).classList.add("active");
            });
        });
    });

    // Hearing form: only ask for a place when the hearing is physical
    document.querySelectorAll("[data-mode-select]").forEach(function (select) {
        function update() {
            const form = select.closest("form");
            form.querySelectorAll("[data-show-for]").forEach(function (el) {
                el.classList.toggle("hide", el.dataset.showFor !== select.value);
            });
        }
        select.addEventListener("change", update);
        update();
    });
});

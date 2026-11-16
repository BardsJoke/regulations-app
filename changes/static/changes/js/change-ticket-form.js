
(function () {
    "use strict";

    function initializePackageFiltering(form) {
        const targetSelect = form.querySelector(
            '[data-change-ticket-target="true"]'
        );

        const packageSelect = form.querySelector(
            '[data-change-ticket-package="true"]'
        );

        if (!targetSelect || !packageSelect) {
            return;
        }

        // Не подключаем обработчики повторно.
        if (form.dataset.packageFilteringInitialized === "true") {
            return;
        }

        form.dataset.packageFilteringInitialized = "true";

        function updatePackages() {
            const selectedTarget =
                targetSelect.options[targetSelect.selectedIndex];

            const selectedSoftwareId =
                selectedTarget?.dataset.softwareId || "";

            Array.from(packageSelect.options).forEach((option) => {
                // Пустой вариант выбора всегда доступен.
                if (!option.value) {
                    option.hidden = false;
                    option.disabled = false;
                    return;
                }

                const matchesSoftware =
                    selectedSoftwareId !== "" &&
                    option.dataset.softwareId === selectedSoftwareId;

                option.hidden = !matchesSoftware;
                option.disabled = !matchesSoftware;

                // Убираем пакет, если пользователь сменил ПО.
                if (!matchesSoftware && option.selected) {
                    packageSelect.value = "";
                }
            });

            packageSelect.disabled = selectedSoftwareId === "";

            if (selectedSoftwareId === "") {
                packageSelect.value = "";
            }
        }

        targetSelect.addEventListener(
            "change",
            updatePackages
        );

        updatePackages();
    }

    function initializeForms() {
        document.querySelectorAll("form").forEach(
            initializePackageFiltering
        );
    }

    document.addEventListener(
        "DOMContentLoaded",
        initializeForms
    );

    // На случай повторной инициализации интерфейса Wagtail.
    document.addEventListener(
        "wagtail:ready",
        initializeForms
    );

})();

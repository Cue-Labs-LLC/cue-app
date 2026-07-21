/*
 * CueAddressAutocomplete
 * ----------------------
 * Custom address autocomplete built directly on the Google Places API (New).
 * Replaces the stock <gmp-place-autocomplete> web component so we fully own the
 * markup and styling (the stock component renders its dropdown in a shadow DOM
 * that does not reliably accept our theme, producing unreadable dark-on-dark text).
 *
 * Usage:
 *   await CueAddressAutocomplete.loadApi(apiKey);
 *   CueAddressAutocomplete.attach({
 *     inputEl:    <input> or id,
 *     dropdownEl: <div> or id,
 *     fields: { street, city, state, postal, country },  // <input> els or ids
 *     onSelect: function(formattedAddress) {},           // optional
 *     onClear:  function() {},                            // optional
 *   });
 */
(function () {
    'use strict';

    var apiPromise = null;
    var placesLibPromise = null;

    function el(ref) {
        if (!ref) return null;
        return (typeof ref === 'string') ? document.getElementById(ref) : ref;
    }

    // Load the Maps JS API exactly once per page, then resolve the places library.
    function loadApi(apiKey) {
        if (placesLibPromise) return placesLibPromise;

        if (!apiPromise) {
            apiPromise = new Promise(function (resolve, reject) {
                if (window.google && window.google.maps && window.google.maps.importLibrary) {
                    resolve();
                    return;
                }
                var s = document.createElement('script');
                s.src = 'https://maps.googleapis.com/maps/api/js?key=' +
                    encodeURIComponent(apiKey || '') + '&libraries=places';
                s.async = true;
                s.defer = true;
                s.onload = function () { resolve(); };
                s.onerror = function () { reject(new Error('Failed to load Google Maps JS API')); };
                document.head.appendChild(s);
            });
        }

        placesLibPromise = apiPromise.then(function () {
            return google.maps.importLibrary('places');
        });
        return placesLibPromise;
    }

    // Parse a Places (New) addressComponents array into our flat fields.
    function parseComponents(place) {
        var components = (place && place.addressComponents) || [];
        var streetNumber = '', route = '', city = '', state = '', postalCode = '', country = '';
        for (var i = 0; i < components.length; i++) {
            var c = components[i];
            var types = c.types || [];
            if (types.indexOf('street_number') !== -1) streetNumber = c.longText || '';
            else if (types.indexOf('route') !== -1) route = c.longText || '';
            else if (types.indexOf('locality') !== -1) city = c.longText || '';
            else if (types.indexOf('administrative_area_level_1') !== -1) state = c.shortText || '';
            else if (types.indexOf('postal_code') !== -1) postalCode = c.longText || '';
            else if (types.indexOf('country') !== -1) country = c.longText || '';
        }
        var street = '';
        if (streetNumber && route) street = streetNumber + ' ' + route;
        else if (route) street = route;
        else if (streetNumber) street = streetNumber;
        if (!street && place && place.formattedAddress) {
            var firstComma = place.formattedAddress.indexOf(',');
            street = firstComma !== -1
                ? place.formattedAddress.slice(0, firstComma).trim()
                : place.formattedAddress.trim();
        }
        return { street: street, city: city, state: state, postal: postalCode, country: country };
    }

    function attach(opts) {
        var inputEl = el(opts.inputEl);
        var dropdownEl = el(opts.dropdownEl);
        if (!inputEl || !dropdownEl) return;

        var f = opts.fields || {};
        var fields = {
            street: el(f.street),
            city: el(f.city),
            state: el(f.state),
            postal: el(f.postal),
            country: el(f.country),
        };

        var sessionToken = null;
        var suggestions = [];
        var activeIndex = -1;
        var debounceTimer = null;
        var lastQuery = '';

        dropdownEl.classList.add('cue-autocomplete-dropdown');
        dropdownEl.setAttribute('role', 'listbox');
        inputEl.setAttribute('autocomplete', 'off');
        inputEl.setAttribute('role', 'combobox');
        inputEl.setAttribute('aria-expanded', 'false');
        inputEl.setAttribute('aria-autocomplete', 'list');

        function ensureToken(lib) {
            if (!sessionToken) sessionToken = new lib.AutocompleteSessionToken();
            return sessionToken;
        }

        function closeDropdown() {
            dropdownEl.innerHTML = '';
            dropdownEl.classList.remove('cue-autocomplete-open');
            inputEl.setAttribute('aria-expanded', 'false');
            activeIndex = -1;
            suggestions = [];
        }

        function clearFields() {
            ['street', 'city', 'state', 'postal', 'country'].forEach(function (k) {
                if (fields[k]) fields[k].value = '';
            });
            if (typeof opts.onClear === 'function') opts.onClear();
        }

        function setActive(idx) {
            var rows = dropdownEl.querySelectorAll('.cue-autocomplete-item');
            for (var i = 0; i < rows.length; i++) {
                rows[i].classList.toggle('cue-autocomplete-item--active', i === idx);
                if (i === idx) rows[i].setAttribute('aria-selected', 'true');
                else rows[i].removeAttribute('aria-selected');
            }
            activeIndex = idx;
            if (idx >= 0 && rows[idx]) {
                rows[idx].scrollIntoView({ block: 'nearest' });
            }
        }

        function render() {
            dropdownEl.innerHTML = '';
            if (!suggestions.length) {
                closeDropdown();
                return;
            }
            suggestions.forEach(function (suggestion, idx) {
                var pred = suggestion.placePrediction;
                if (!pred) return;
                var main = (pred.mainText && pred.mainText.text) ||
                    (pred.text && pred.text.text) || '';
                var secondary = (pred.secondaryText && pred.secondaryText.text) || '';

                var item = document.createElement('button');
                item.type = 'button';
                item.className = 'cue-autocomplete-item';
                item.setAttribute('role', 'option');

                var mainSpan = document.createElement('span');
                mainSpan.className = 'cue-autocomplete-item-main';
                mainSpan.textContent = main;
                item.appendChild(mainSpan);

                if (secondary) {
                    var secSpan = document.createElement('span');
                    secSpan.className = 'cue-autocomplete-item-secondary';
                    secSpan.textContent = secondary;
                    item.appendChild(secSpan);
                }

                item.addEventListener('mouseenter', function () { setActive(idx); });
                // mousedown (not click) so selection fires before the input blur closes us.
                item.addEventListener('mousedown', function (e) {
                    e.preventDefault();
                    selectSuggestion(suggestion);
                });
                dropdownEl.appendChild(item);
            });
            dropdownEl.classList.add('cue-autocomplete-open');
            inputEl.setAttribute('aria-expanded', 'true');
            setActive(-1);
        }

        function selectSuggestion(suggestion) {
            var pred = suggestion && suggestion.placePrediction;
            if (!pred || typeof pred.toPlace !== 'function') return;
            var place = pred.toPlace();
            place.fetchFields({ fields: ['addressComponents', 'formattedAddress'] })
                .then(function () {
                    var parsed = parseComponents(place);
                    if (fields.street) fields.street.value = parsed.street;
                    if (fields.city) fields.city.value = parsed.city;
                    if (fields.state) fields.state.value = parsed.state;
                    if (fields.postal) fields.postal.value = parsed.postal;
                    if (fields.country) fields.country.value = parsed.country;

                    var formatted = place.formattedAddress || parsed.street;
                    inputEl.value = formatted;
                    if (typeof opts.onSelect === 'function') opts.onSelect(formatted, parsed);

                    // Start a fresh billing session after a completed selection.
                    sessionToken = null;
                    closeDropdown();
                })
                .catch(function (e) {
                    if (window.console && console.error) console.error('fetchFields failed', e);
                });
        }

        function fetchSuggestions(query) {
            loadApi(opts.apiKey).then(function (lib) {
                ensureToken(lib);
                return lib.AutocompleteSuggestion.fetchAutocompleteSuggestions({
                    input: query,
                    sessionToken: sessionToken,
                });
            }).then(function (res) {
                // Ignore stale responses if the input changed while we awaited.
                if (query !== lastQuery) return;
                suggestions = (res && res.suggestions) || [];
                render();
            }).catch(function (e) {
                if (window.console && console.error) console.error('fetchAutocompleteSuggestions failed', e);
                closeDropdown();
            });
        }

        inputEl.addEventListener('input', function () {
            var query = inputEl.value.trim();
            lastQuery = query;
            // Any edit invalidates a previously-selected address.
            clearFields();
            if (debounceTimer) clearTimeout(debounceTimer);
            if (!query) { closeDropdown(); return; }
            debounceTimer = setTimeout(function () { fetchSuggestions(query); }, 200);
        });

        inputEl.addEventListener('keydown', function (e) {
            var open = dropdownEl.classList.contains('cue-autocomplete-open');
            if (e.key === 'ArrowDown') {
                if (!open) return;
                e.preventDefault();
                setActive(Math.min(activeIndex + 1, suggestions.length - 1));
            } else if (e.key === 'ArrowUp') {
                if (!open) return;
                e.preventDefault();
                setActive(Math.max(activeIndex - 1, 0));
            } else if (e.key === 'Enter') {
                if (open && activeIndex >= 0 && suggestions[activeIndex]) {
                    e.preventDefault();
                    selectSuggestion(suggestions[activeIndex]);
                }
            } else if (e.key === 'Escape') {
                closeDropdown();
            }
        });

        inputEl.addEventListener('blur', function () {
            // Delay so a mousedown selection can run first.
            setTimeout(closeDropdown, 120);
        });

        document.addEventListener('click', function (e) {
            if (e.target !== inputEl && !dropdownEl.contains(e.target)) {
                closeDropdown();
            }
        });
    }

    window.CueAddressAutocomplete = {
        loadApi: loadApi,
        attach: attach,
        parseComponents: parseComponents,
    };
})();

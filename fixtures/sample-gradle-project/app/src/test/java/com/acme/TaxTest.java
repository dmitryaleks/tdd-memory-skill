package com.acme;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

/** Same package as the target, but unrelated to it. */
class TaxTest {

    @Test
    void appliesStandardRate() {
        assertEquals(120, Tax.withVat(100));
    }
}

package com.acme.support;

import com.acme.Order;

/** A helper, not a test: it mentions Order but contains no @Test. */
public final class TestData {

    private TestData() {
    }

    public static Order orderOf(int amount) {
        Order order = new Order();
        order.addLine(amount);
        return order;
    }
}

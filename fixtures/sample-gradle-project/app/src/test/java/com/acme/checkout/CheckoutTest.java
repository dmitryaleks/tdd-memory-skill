package com.acme.checkout;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.acme.Order;
import org.junit.jupiter.api.Test;

/** Different package, but exercises the target through its public API. */
class CheckoutTest {

    @Test
    void chargesThePayableAmount() {
        Order order = new Order();
        order.addLine(200);
        assertEquals(180, order.payable());
    }
}
